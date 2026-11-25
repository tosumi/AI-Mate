// ペルソナデザイナの画面(/designer。本体仕様書 §15.1 の M10・M10-2。2026-11-25 のユーザーの決定)。
// - 一般ユーザーのサインイン(会話の画面と同じ)。入口は当面 URL だけ。
// - トップは「データの確認」: 新規作成(→ ID と名前 → 作り方の選択)か、保存データの読み込み(ファイル・サンプル → 必ずデザイナ。
//   途中から続けるものは、途中のときと同じ作り方で開く)。
// - 作り方は「ウィザード」(質問に答えるだけ。既定)と「デザイナ」(詳細設定)。切り替えるときは、確かめてから。
// - 下書きはサーバーに保存しない。YAML の保存・読み込みと、タブを閉じるまでの自動の一時保存だけ。
// - サンプルは参考(読み取り専用)。読み込むときに、自分の ID と名前のコピーにする。

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ErrorBoundary } from "../ErrorBoundary";
import { NewVersionBanner } from "../NewVersionBanner";
import { createAuth, signInErrorMessage } from "../auth";
import { loadConfig } from "../config";
import { type CheckResult, type Choices, type Definition, DesignerApi } from "./api";
import { Compare } from "./Compare";
import {
  type Draft, fileName, isOwnId, loadDraft, loadWizard, type Mode, SAMPLE_PREFIX, saveDraft, saveWizard, setIn, type WizardState,
} from "./draft";
import { TextField } from "./fields";
import { Modal } from "./Modal";
import { Preview } from "./Preview";
import { Section, SECTIONS, type SectionId } from "./Sections";
import { pathCandidates, sectionForPath } from "./sectionList";
import { Simulation } from "./Simulation";
import { useHeightVar } from "./heightVar";
import { Toolbar } from "./Toolbar";
import { Types } from "./Types";
import { WizardRunner } from "./WizardRunner";
import "./designer.css";

type Phase = "loading" | "signed-out" | "home" | "common" | "mode" | "wizard" | "main" | "error";
type Page = "edit" | "simulate" | "compare" | "types";
const PAGES: { id: Page; label: string }[] = [
  { id: "edit", label: "編集" },
  { id: "simulate", label: "シミュレーション" },
  { id: "compare", label: "比較" },
  { id: "types", label: "タイプ一覧" },
];
const CHECK_DELAY_MS = 400;
const NEW_TYPE = "iaef"; // デザイナで新しく作るときの性格タイプ(CLI の persona new の既定と同じ)
const ID_HINT = "英小文字で始まる 2〜41 文字(英小文字・数字・_)。sample_ で始まる ID は使えません。";

/** 定義を YAML にして、端末に保存する(ダウンロード)。保存したファイル名を返す。 */
async function downloadYaml(api: DesignerApi, definition: Definition): Promise<string> {
  const { yaml } = await api.dumpYaml(definition);
  const url = URL.createObjectURL(new Blob([yaml], { type: "text/yaml" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = fileName(definition);
  a.click();
  URL.revokeObjectURL(url);
  return fileName(definition);
}

export function DesignerApp() {
  const [config, configError] = useMemo((): [ReturnType<typeof loadConfig> | null, string] => {
    try {
      return [loadConfig(), ""];
    } catch (e) {
      return [null, (e as Error).message];
    }
  }, []);
  const auth = useMemo(() => (config ? createAuth(config) : null), [config]);
  const api = useMemo(() => (config && auth ? new DesignerApi(config.apiUrl, () => auth.token()) : null), [config, auth]);
  const [phase, setPhase] = useState<Phase>(configError ? "error" : "loading");
  const [message, setMessage] = useState(configError);
  const [choices, setChoices] = useState<Choices | null>(null);
  const [draft, setDraft] = useState<Draft | null>(() => loadDraft());
  const [wizard, setWizard] = useState<WizardState | null>(() => loadWizard());
  const [common, setCommon] = useState({ character_id: "my_character", name: "" });
  const [page, setPage] = useState<Page>("edit");
  const [openAt, setOpenAt] = useState<string | null>(null); // デザイナで開いたときに移る先(検査の結果の場所)
  const [signingIn, setSigningIn] = useState(false);
  const header = useRef<HTMLElement>(null);
  useHeightVar(header, "--dz-header-h");

  useEffect(() => {
    document.title = "MyCompanion デザイナ";
  }, []);

  const afterSignIn = useCallback(async () => {
    setChoices(await api!.choices());
    setPhase("home");
  }, [api]);

  useEffect(() => {
    if (!auth || !api) return;
    (async () => {
      try {
        if (!(await auth.start())) {
          setPhase("signed-out");
          return;
        }
        await afterSignIn();
      } catch (e) {
        setMessage((e as Error).message);
        setPhase("error");
      }
    })();
  }, [auth, api, afterSignIn]);

  // 下書き・ウィザードの途中は、変わるたびに、その場でタブの一時保存領域に残す(再読み込みで消えないように)。
  // 描画のあとの effect で残すと、入力の直後に読み込み直したとき、最後の変更が残らないことがある
  const updateDraft = useCallback((next: Draft | null) => {
    saveDraft(next);
    setDraft(next);
  }, []);
  const updateWizard = useCallback((next: WizardState | null) => {
    saveWizard(next);
    setWizard(next);
  }, []);

  /** デザイナで開く(保存データの読み込み・ウィザードのあと)。ウィザードの途中は捨てる。 */
  const openInDesigner = (definition: Definition, source: string, jumpTo?: string) => {
    setOpenAt(jumpTo ?? null);
    updateWizard(null);
    updateDraft({ definition, baseline: definition, source });
    setPage("edit");
    setPhase("main");
  };

  /** ウィザードを最初から(ID と名前だけを引き継ぐ)。デザイナの下書きは捨てる。 */
  const startWizard = (character_id: string, name: string) => {
    updateDraft(null);
    updateWizard({ character_id, name, answers: [] });
    setPhase("wizard");
  };

  const chooseMode = async (mode: Mode) => {
    if (mode === "wizard") {
      startWizard(common.character_id, common.name.trim());
      return;
    }
    try {
      const r = await api!.create({ character_id: common.character_id, name: common.name.trim(), type_id: NEW_TYPE, profile: null });
      openInDesigner(r.definition, "新規");
    } catch (e) {
      setMessage((e as Error).message);
    }
  };

  const signedIn = phase !== "loading" && phase !== "signed-out" && phase !== "error";
  return (
    <div className="dz">
      <header className="dz-header" ref={header}>
        <span className="dz-brand">MyCompanion <span className="dz-badge">デザイナ</span></span>
        {phase === "main" && (
          <nav aria-label="デザイナの画面" className="dz-pages">
            {PAGES.map((p) => (
              <button key={p.id} className={page === p.id ? "active" : undefined} aria-current={page === p.id ? "page" : undefined} onClick={() => setPage(p.id)}>
                {p.label}
              </button>
            ))}
          </nav>
        )}
        {signedIn && auth && (
          <button className="dz-signout" onClick={() => auth.signOut().then(() => setPhase("signed-out"))}>サインアウト</button>
        )}
      </header>
      <NewVersionBanner className="dz-update-banner" />
      <main className="dz-main">
        <ErrorBoundary>
          {phase === "loading" && <p aria-busy="true">読み込んでいます…</p>}
          {phase === "error" && <p role="alert" className="dz-error">{message}</p>}
          {phase === "signed-out" && auth && (
            <section className="dz-center">
              <h1>キャラクタのデザイナ</h1>
              <p>AI のキャラクタの性格・話し方・感情の動き方を設計して、模擬の会話の場面で確かめられます。作ったキャラクタは、YAML のファイルとしてあなたの端末に保存します(サーバーには保存しません)。</p>
              <button className="dz-primary" disabled={signingIn} onClick={async () => {
                setSigningIn(true);
                setMessage("");
                try {
                  if (await auth.signIn()) await afterSignIn();
                  setSigningIn(false);
                } catch (e) {
                  setMessage(signInErrorMessage(e));
                  setSigningIn(false);
                }
              }}>
                {signingIn ? "サインインの画面へ移動しています…" : "メールでサインイン"}
              </button>
              {message && <p role="alert">{message}</p>}
            </section>
          )}
          {phase === "home" && api && (
            <Home api={api} draft={draft} wizard={wizard} onNew={() => setPhase("common")} onOpen={openInDesigner}
              onResumeDraft={() => {
                setOpenAt(null);
                setPhase("main");
              }} onResumeWizard={() => setPhase("wizard")} />
          )}
          {phase === "common" && (
            <Common value={common} onChange={setCommon} onBack={() => setPhase("home")} onNext={() => {
              setMessage("");
              setPhase("mode");
            }} />
          )}
          {phase === "mode" && (
            <>
              <ModeChoice onChoose={(m) => void chooseMode(m)} onBack={() => setPhase("common")} />
              {message && <p role="alert" className="dz-error">{message}</p>}
            </>
          )}
          {phase === "wizard" && api && choices && wizard && (
            <>
              {message && <p role="alert" className="dz-error">{message}</p>}
              <WizardRunner api={api} choices={choices} state={wizard} onChange={updateWizard} onOpenDesigner={openInDesigner}
                onRestart={() => setPhase("home")} onSave={(d) => void downloadYaml(api, d).catch((e) => setMessage((e as Error).message))} />
            </>
          )}
          {phase === "main" && api && choices && draft && (
            <Workspace api={api} choices={choices} draft={draft} setDraft={updateDraft} page={page} openAt={openAt}
              onSwitchToWizard={startWizard} onRestart={() => setPhase("home")} />
          )}
        </ErrorBoundary>
      </main>
    </div>
  );
}

/** トップ(データの確認): 新規作成か、保存データの読み込み(YAML・サンプルのコピー・編集中の下書き)。 */
function Home({ api, draft, wizard, onNew, onOpen, onResumeDraft, onResumeWizard }: {
  api: DesignerApi; draft: Draft | null; wizard: WizardState | null; onNew: () => void;
  onOpen: (d: Definition, source: string) => void; onResumeDraft: () => void; onResumeWizard: () => void;
}) {
  const [samples, setSamples] = useState<{ character_id: string; name: string }[]>([]);
  const [copying, setCopying] = useState<{ character_id: string; name: string } | null>(null);
  const [message, setMessage] = useState("");

  useEffect(() => {
    api.samples().then((r) => setSamples(r.samples), (e) => setMessage((e as Error).message));
  }, [api]);

  return (
    <section className="dz-start" aria-labelledby="dz-home-title">
      <h1 id="dz-home-title">データの確認</h1>
      <div className="dz-start-grid">
        <fieldset className="dz-group">
          <legend>新規作成</legend>
          <p className="dz-hint">新しいキャラクタを作ります。ID と名前を決めたあと、作り方(質問に答える / 細かく設定する)を選びます。</p>
          <button className="dz-primary" onClick={onNew}>新しく作る</button>
        </fieldset>
        <fieldset className="dz-group">
          <legend>保存データの読み込み</legend>
          {(wizard || draft) && (
            <>
              <p className="dz-hint">途中から続ける(途中のときと同じ作り方で開きます):</p>
              {wizard && (
                <button onClick={onResumeWizard}>ウィザードの続きから({wizard.name}・{wizard.answers.length} 問まで回答済み)</button>
              )}
              {draft && <button onClick={onResumeDraft}>デザイナの下書きに戻る({String(draft.definition.name ?? "")})</button>}
            </>
          )}
          <p className="dz-hint">ファイル・サンプルを開く(デザイナ(詳細設定)で開きます):</p>
          <YamlOpen api={api} label="YAML のファイルを開く" onOpen={onOpen} onError={setMessage} />
          <p className="dz-hint">サンプル(参考。自分の ID と名前のコピーとして開きます):</p>
          <ul className="dz-list">
            {samples.map((s) => (
              <li key={s.character_id}>
                <button onClick={() => setCopying(s)}>{s.name}をコピーして開く</button>
              </li>
            ))}
          </ul>
        </fieldset>
      </div>
      {copying && (
        <SampleCopy api={api} sample={copying} onCancel={() => setCopying(null)} onOpen={onOpen} onError={setMessage} />
      )}
      {message && <p role="alert" className="dz-error">{message}</p>}
    </section>
  );
}

/** サンプルを、自分の ID と名前のコピーとして開く(サンプルそのものは変えられない)。 */
function SampleCopy({ api, sample, onCancel, onOpen, onError }: {
  api: DesignerApi; sample: { character_id: string; name: string }; onCancel: () => void;
  onOpen: (d: Definition, source: string) => void; onError: (m: string) => void;
}) {
  const [id, setId] = useState("");
  const [name, setName] = useState("");
  return (
    <Modal labelledBy="dz-copy-title" onCancel={onCancel}>
      <h3 id="dz-copy-title">{sample.name}をコピーして開く</h3>
      <p>サンプルは変えられないため、自分の ID と名前を付けたコピーを開きます。</p>
      <TextField label="キャラクタの ID" value={id} maxLength={41} onChange={setId} hint={id && !isOwnId(id) ? ID_HINT : undefined} />
      <TextField label="名前" value={name} maxLength={40} onChange={setName} />
      <div className="dz-row">
        <button className="dz-primary" disabled={!isOwnId(id) || !name.trim()} onClick={async () => {
          try {
            const r = await api.sample(sample.character_id);
            onOpen({ ...r.definition, character_id: id, name: name.trim() }, `サンプル(${sample.name})のコピー`);
          } catch (e) {
            onError((e as Error).message);
          }
        }}>コピーして開く</button>
        <button onClick={onCancel}>やめる</button>
      </div>
    </Modal>
  );
}

/** 共通データ(ID と名前)。どちらの作り方でも、最初に決める。 */
function Common({ value, onChange, onBack, onNext }: {
  value: { character_id: string; name: string }; onChange: (v: { character_id: string; name: string }) => void;
  onBack: () => void; onNext: () => void;
}) {
  const ok = isOwnId(value.character_id) && !!value.name.trim();
  return (
    <section className="dz-center" aria-labelledby="dz-common-title">
      <h1 id="dz-common-title">ID と名前</h1>
      <TextField label="キャラクタの ID" value={value.character_id} maxLength={41}
        onChange={(v) => onChange({ ...value, character_id: v })} hint={isOwnId(value.character_id) ? "英小文字・数字・_。保存するファイルの名前にもなります。" : ID_HINT} />
      <TextField label="名前" value={value.name} maxLength={40} onChange={(v) => onChange({ ...value, name: v })} />
      <div className="dz-wizard-nav">
        <button onClick={onBack}>戻る</button>
        <button className="dz-primary" disabled={!ok} onClick={onNext}>次へ</button>
      </div>
    </section>
  );
}

/** 作り方の選択(必ず選んでもらう。既定は「ウィザード」に焦点を置き、クリック・スペース・改行の1回で進める)。 */
function ModeChoice({ onChoose, onBack }: { onChoose: (mode: Mode) => void; onBack: () => void }) {
  const first = useRef<HTMLButtonElement>(null);
  useEffect(() => first.current?.focus(), []);
  return (
    <section className="dz-center" aria-labelledby="dz-mode-title">
      <h1 id="dz-mode-title">作り方を選んでください</h1>
      <div className="dz-mode-choices">
        <button ref={first} className="dz-mode-card" onClick={() => onChoose("wizard")}>
          <strong>ウィザード(既定)</strong>
          <span>いくつかの質問(35問ほど)に答えるだけで、キャラクタができあがります。初めての方向け。</span>
        </button>
        <button className="dz-mode-card" onClick={() => onChoose("designer")}>
          <strong>デザイナ(詳細設定)</strong>
          <span>性格・口数・感情の動き・好き嫌いなどを、1つ1つ自分で細かく設定します。右側に、結果がすぐに出ます。</span>
        </button>
      </div>
      <div className="dz-wizard-nav">
        <button onClick={onBack}>戻る</button>
      </div>
    </section>
  );
}

function YamlOpen({ api, onOpen, onError, label = "ファイルを選ぶ" }: {
  api: DesignerApi; onOpen: (d: Definition, source: string) => void; onError: (m: string) => void; label?: string;
}) {
  return (
    <label className="dz-file">
      {label}
      <input type="file" accept=".yaml,.yml,text/yaml" onChange={async (e) => {
        const file = e.target.files?.[0];
        e.target.value = "";
        if (!file) return;
        try {
          const r = await api.parseYaml(await file.text());
          onOpen(r.definition, file.name);
        } catch (err) {
          onError((err as Error).message);
        }
      }} />
    </label>
  );
}

function Workspace({ api, choices, draft, setDraft, page, openAt, onSwitchToWizard, onRestart }: {
  api: DesignerApi; choices: Choices; draft: Draft; setDraft: (d: Draft) => void; page: Page; openAt: string | null;
  onSwitchToWizard: (character_id: string, name: string) => void; onRestart: () => void;
}) {
  const [check, setCheck] = useState<CheckResult | null>(null);
  const [checking, setChecking] = useState(false);
  const [checkError, setCheckError] = useState("");
  const [section, setSection] = useState<SectionId>(() => (openAt && sectionForPath(openAt)) || "basic");
  const [saving, setSaving] = useState<null | "ask">(null);
  const [newId, setNewId] = useState("");
  const [toWizard, setToWizard] = useState<{ character_id: string; name: string } | null>(null);
  const [message, setMessage] = useState("");
  const [jump, setJump] = useState<{ path: string; n: number } | null>(() => (openAt ? { path: openAt, n: 1 } : null));
  const editor = useRef<HTMLDivElement>(null);
  const definition = draft.definition;

  // 検査の結果を押したとき: 原因の設定の項目に切り替え、その欄(なければ親の欄、項目の見出し)へ移る
  const jumpTo = (path: string) => {
    const target = sectionForPath(path);
    if (!target) return;
    setSection(target);
    setJump((j) => ({ path, n: (j?.n ?? 0) + 1 }));
  };
  useEffect(() => {
    if (!jump || !editor.current) return;
    const root = editor.current;
    const frame = window.requestAnimationFrame(() => {
      const element = pathCandidates(jump.path)
        .map((p) => root.querySelector<HTMLElement>(`[data-path="${CSS.escape(p)}"]`))
        .find((e) => e) ?? root.querySelector<HTMLElement>("h2");
      if (!element) return;
      element.scrollIntoView({ block: "center", behavior: "smooth" });
      element.querySelector<HTMLElement>("input, select, textarea")?.focus({ preventScroll: true });
      element.classList.add("dz-flash");
      window.setTimeout(() => element.classList.remove("dz-flash"), 2000);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [jump]);

  // 編集のたびに、少し待ってから検査する(打っている途中で何度も呼ばない)
  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      setChecking(true);
      try {
        setCheck(await api.check(definition, controller.signal));
        setCheckError("");
      } catch (e) {
        if ((e as Error).name !== "AbortError") setCheckError((e as Error).message);
      } finally {
        setChecking(false);
      }
    }, CHECK_DELAY_MS);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [api, definition]);

  const edit = (path: (string | number)[], value: unknown) => setDraft({ ...draft, definition: setIn(definition, path, value) });
  const usesSample = String(definition.character_id ?? "").startsWith(SAMPLE_PREFIX);

  async function download(def: Definition) {
    setMessage("");
    try {
      const name = await downloadYaml(api, def);
      setDraft({ ...draft, definition: def, baseline: def });
      setMessage(`保存しました(${name})。`);
    } catch (e) {
      setMessage((e as Error).message);
    }
  }

  const toolbar = (
    <Toolbar name={String(definition.name ?? "")} id={String(definition.character_id ?? "")}>
      <button className="dz-primary" onClick={() => (usesSample ? setSaving("ask") : void download(definition))}>YAML を保存</button>
      <YamlOpen api={api} label="YAML を開く" onOpen={(d, source) => setDraft({ definition: d, baseline: d, source })} onError={setMessage} />
      <button onClick={onRestart}>新しく始める</button>
      <button onClick={() => setToWizard({ character_id: String(definition.character_id ?? ""), name: String(definition.name ?? "") })}>
        ウィザードに切り替え
      </button>
    </Toolbar>
  );

  // デザイナ → ウィザード: ID と名前のほかは、すべて捨ててよいかを確かめる
  const wizardAsk = toWizard && (
    <Modal labelledBy="dz-to-wizard-title" onCancel={() => setToWizard(null)}>
      <h3 id="dz-to-wizard-title">ウィザードに切り替えますか</h3>
      <p className="dz-erase">IDと名前以外は、<strong>すべて消えます</strong>。</p>
      <p>ウィザードは、質問に最初から答えて、キャラクタを組み立て直します。残したいときは、先に「YAML を保存」してください。</p>
      <TextField label="キャラクタの ID" value={toWizard.character_id} maxLength={41} onChange={(v) => setToWizard({ ...toWizard, character_id: v })}
        hint={isOwnId(toWizard.character_id) ? undefined : ID_HINT} />
      <TextField label="名前" value={toWizard.name} maxLength={40} onChange={(v) => setToWizard({ ...toWizard, name: v })} />
      <div className="dz-row">
        <button className="dz-danger" disabled={!isOwnId(toWizard.character_id) || !toWizard.name.trim()}
          onClick={() => onSwitchToWizard(toWizard.character_id, toWizard.name.trim())}>設定を消して、ウィザードを始める</button>
        <button onClick={() => setToWizard(null)}>やめる(デザイナを続ける)</button>
      </div>
    </Modal>
  );

  const saveAsk = saving === "ask" && (
    <Modal labelledBy="dz-save-title" onCancel={() => setSaving(null)}>
      <h3 id="dz-save-title">自分の ID で保存</h3>
      <p>サンプルは変えられないため、自分の ID を付けて保存します。</p>
      <TextField label="新しい ID" value={newId} maxLength={41} onChange={setNewId} />
      <div className="dz-row">
        <button className="dz-primary" disabled={!isOwnId(newId)}
          onClick={() => {
            setSaving(null);
            void download(setIn(definition, ["character_id"], newId));
          }}>この ID で保存</button>
        <button onClick={() => setSaving(null)}>やめる</button>
      </div>
    </Modal>
  );

  const dialogs = <>{wizardAsk}{saveAsk}</>;
  if (page === "simulate") return <>{toolbar}{dialogs}<Simulation api={api} definition={definition} canRun={!!check?.ok} /></>;
  if (page === "compare") return <>{toolbar}{dialogs}<Compare api={api} choices={choices} definition={definition} baseline={draft.baseline} current={check} /></>;
  if (page === "types") return <>{toolbar}{dialogs}<Types api={api} choices={choices} /></>;

  return (
    <>
      {toolbar}
      {dialogs}
      {message && <p role="status" className="dz-message">{message}</p>}
      <div className="dz-layout">
        <nav className="dz-sections" aria-label="設定の項目">
          {SECTIONS.map((s) => (
            <button key={s.id} className={section === s.id ? "active" : undefined} aria-current={section === s.id ? "true" : undefined} onClick={() => setSection(s.id)}>
              <span>{s.label}</span><span className="dz-hint">{s.hint}</span>
            </button>
          ))}
        </nav>
        <div className="dz-editor" ref={editor}>
          <h2 tabIndex={-1}>{SECTIONS.find((s) => s.id === section)?.label}</h2>
          <Section id={section} def={definition} edit={edit} choices={choices} api={api} />
        </div>
        <Preview check={check} busy={checking} error={checkError} onJump={jumpTo} />
      </div>
    </>
  );
}
