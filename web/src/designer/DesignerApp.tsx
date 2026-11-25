// ペルソナデザイナの画面(/designer。本体仕様書 §15.1 の M10。2026-11-25 のユーザーの決定)。
// - 一般ユーザーのサインイン(会話の画面と同じ)。入口は当面 URL だけ。
// - 最初に「デザイナ」(1画面。既定)か「ウィザード」(1ページずつ)を選ぶ。
// - 下書きはサーバーに保存しない。YAML の保存・読み込みと、タブを閉じるまでの自動の一時保存だけ。
// - サンプルは参考(読み取り専用)。保存するときは、自分の ID に変える。

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ErrorBoundary } from "../ErrorBoundary";
import { NewVersionBanner } from "../NewVersionBanner";
import { createAuth, signInErrorMessage } from "../auth";
import { loadConfig } from "../config";
import { type CheckResult, type Choices, type Definition, DesignerApi } from "./api";
import { Compare } from "./Compare";
import { type Draft, fileName, loadDraft, loadMode, type Mode, SAMPLE_PREFIX, saveDraft, saveMode, setIn } from "./draft";
import { Select, TextField } from "./fields";
import { Preview } from "./Preview";
import { Section, SECTIONS, type SectionId, type SectionProps } from "./Sections";
import { Simulation } from "./Simulation";
import { Types } from "./Types";
import "./designer.css";

type Phase = "loading" | "signed-out" | "mode" | "start" | "main" | "error";
type Page = "edit" | "simulate" | "compare" | "types";
const PAGES: { id: Page; label: string }[] = [
  { id: "edit", label: "編集" },
  { id: "simulate", label: "シミュレーション" },
  { id: "compare", label: "比較" },
  { id: "types", label: "タイプ一覧" },
];
const CHECK_DELAY_MS = 400;

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
  const [mode, setMode] = useState<Mode>(() => loadMode() ?? "designer");
  const [draft, setDraft] = useState<Draft | null>(() => loadDraft());
  const [page, setPage] = useState<Page>("edit");
  const [signingIn, setSigningIn] = useState(false);

  useEffect(() => {
    document.title = "MyCompanion デザイナ";
  }, []);

  const afterSignIn = useCallback(async () => {
    setChoices(await api!.choices());
    setPhase("mode");
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

  // 下書きは、変わるたびに、タブの一時保存領域に残す(再読み込みで消えないように)
  useEffect(() => saveDraft(draft), [draft]);

  const chooseMode = (next: Mode) => {
    setMode(next);
    saveMode(next);
    setPhase(draft ? "main" : "start");
  };

  const open = (definition: Definition, source: string) => {
    setDraft({ definition, baseline: definition, source });
    setPage("edit");
    setPhase("main");
  };

  return (
    <div className="dz">
      <header className="dz-header">
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
        {(phase === "main" || phase === "mode" || phase === "start") && auth && (
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
          {phase === "mode" && <ModeChoice initial={mode} onChoose={chooseMode} />}
          {phase === "start" && api && choices && <Start api={api} choices={choices} onOpen={open} hasDraft={!!draft} onResume={() => setPhase("main")} />}
          {phase === "main" && api && choices && draft && (
            <Workspace api={api} choices={choices} draft={draft} setDraft={setDraft} mode={mode} page={page}
              onSwitchMode={() => chooseMode(mode === "designer" ? "wizard" : "designer")} onRestart={() => setPhase("start")} />
          )}
        </ErrorBoundary>
      </main>
    </div>
  );
}

/** 使い方の選択(必ず選んでもらう。既定は「デザイナ」に焦点を置き、クリック・スペース・改行の1回で進める)。 */
function ModeChoice({ initial, onChoose }: { initial: Mode; onChoose: (mode: Mode) => void }) {
  const first = useRef<HTMLButtonElement>(null);
  useEffect(() => first.current?.focus(), []);
  return (
    <section className="dz-center" aria-labelledby="dz-mode-title">
      <h1 id="dz-mode-title">使い方を選んでください</h1>
      <div className="dz-mode-choices">
        <button ref={first} className="dz-mode-card" onClick={() => onChoose("designer")}>
          <strong>デザイナ{initial === "designer" ? "(既定)" : ""}</strong>
          <span>1つの画面で、すべての項目を自由に調整します。右側に、結果がすぐに出ます。</span>
        </button>
        <button className="dz-mode-card" onClick={() => onChoose("wizard")}>
          <strong>ウィザード</strong>
          <span>初めての方向け。コンセプトから順に、1ページずつ決めていきます。</span>
        </button>
      </div>
      <p className="dz-hint">あとから切り替えられます。</p>
    </section>
  );
}

/** 始め方: 新しく作る・サンプルを開く・YAML を開く。 */
function Start({ api, choices, onOpen, hasDraft, onResume }: {
  api: DesignerApi; choices: Choices; onOpen: (d: Definition, source: string) => void; hasDraft: boolean; onResume: () => void;
}) {
  const [samples, setSamples] = useState<{ character_id: string; name: string }[]>([]);
  const [id, setId] = useState("my_character");
  const [name, setName] = useState("");
  const [typeId, setTypeId] = useState(choices.types[0]?.type_id ?? "");
  const [profile, setProfile] = useState("");
  const [message, setMessage] = useState("");

  useEffect(() => {
    api.samples().then((r) => setSamples(r.samples), (e) => setMessage((e as Error).message));
  }, [api]);

  const idOk = /^[a-z][a-z0-9_]{1,40}$/.test(id) && !id.startsWith(SAMPLE_PREFIX);
  return (
    <section className="dz-start" aria-labelledby="dz-start-title">
      <h1 id="dz-start-title">はじめる</h1>
      {hasDraft && <button onClick={onResume}>編集中の下書きに戻る</button>}
      <div className="dz-start-grid">
        <fieldset className="dz-group">
          <legend>新しく作る</legend>
          <TextField label="キャラクタの ID(英小文字で始め、英小文字・数字・_)" value={id} maxLength={41} onChange={setId}
            hint={idOk ? undefined : "英小文字で始まる 2〜41 文字。sample_ で始まる ID は使えません。"} />
          <TextField label="名前" value={name} maxLength={40} onChange={setName} />
          <Select label="性格タイプ" value={typeId} options={choices.types.map((t) => ({ value: t.type_id, label: `${t.type_id} ${t.name}(${t.composition})` }))} onChange={setTypeId} />
          <Select label="表出プロファイル" value={profile} options={[{ value: "", label: "なし" }, ...choices.profiles.map((p) => ({ value: p.id, label: p.name }))]} onChange={setProfile} />
          <button className="dz-primary" disabled={!idOk || !name.trim()} onClick={async () => {
            try {
              const r = await api.create({ character_id: id, name: name.trim(), type_id: typeId, profile: profile || null });
              onOpen(r.definition, "新規");
            } catch (e) {
              setMessage((e as Error).message);
            }
          }}>作る</button>
        </fieldset>
        <fieldset className="dz-group">
          <legend>サンプルを開く(参考)</legend>
          <p className="dz-hint">サンプルは変えられません。開いて調整したものは、自分の ID で保存します。</p>
          <ul className="dz-list">
            {samples.map((s) => (
              <li key={s.character_id}>
                <button onClick={async () => {
                  try {
                    const r = await api.sample(s.character_id);
                    onOpen(r.definition, `サンプル: ${s.name}`);
                  } catch (e) {
                    setMessage((e as Error).message);
                  }
                }}>{s.name}を開く</button>
              </li>
            ))}
          </ul>
        </fieldset>
        <fieldset className="dz-group">
          <legend>YAML を開く</legend>
          <p className="dz-hint">以前に保存したファイルや、CLI の persona.yaml を読み込みます。</p>
          <YamlOpen api={api} onOpen={onOpen} onError={setMessage} />
        </fieldset>
      </div>
      {message && <p role="alert" className="dz-error">{message}</p>}
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

function Workspace({ api, choices, draft, setDraft, mode, page, onSwitchMode, onRestart }: {
  api: DesignerApi; choices: Choices; draft: Draft; setDraft: (d: Draft) => void; mode: Mode; page: Page;
  onSwitchMode: () => void; onRestart: () => void;
}) {
  const [check, setCheck] = useState<CheckResult | null>(null);
  const [checking, setChecking] = useState(false);
  const [checkError, setCheckError] = useState("");
  const [section, setSection] = useState<SectionId>("basic");
  const [step, setStep] = useState(0);
  const [saving, setSaving] = useState<null | "ask">(null);
  const [newId, setNewId] = useState("");
  const [message, setMessage] = useState("");
  const definition = draft.definition;

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
      const { yaml } = await api.dumpYaml(def);
      const url = URL.createObjectURL(new Blob([yaml], { type: "text/yaml" }));
      const a = document.createElement("a");
      a.href = url;
      a.download = fileName(def);
      a.click();
      URL.revokeObjectURL(url);
      setDraft({ ...draft, definition: def, baseline: def });
      setMessage(`保存しました(${fileName(def)})。`);
    } catch (e) {
      setMessage((e as Error).message);
    }
  }

  const toolbar = (
    <div className="dz-toolbar">
      <span className="dz-source">{String(definition.name ?? "")}({String(definition.character_id ?? "")})・{draft.source}</span>
      <button className="dz-primary" onClick={() => (usesSample ? setSaving("ask") : void download(definition))}>YAML を保存</button>
      <YamlOpen api={api} label="YAML を開く" onOpen={(d, source) => setDraft({ definition: d, baseline: d, source })} onError={setMessage} />
      <button onClick={onRestart}>新しく始める</button>
      <button onClick={onSwitchMode}>{mode === "designer" ? "ウィザードに切り替え" : "デザイナに切り替え"}</button>
    </div>
  );

  const saveAsk = saving === "ask" && (
    <div className="dz-dialog" role="dialog" aria-labelledby="dz-save-title">
      <h3 id="dz-save-title">自分の ID で保存</h3>
      <p>サンプルは変えられないため、自分の ID を付けて保存します。</p>
      <TextField label="新しい ID" value={newId} maxLength={41} onChange={setNewId} />
      <div className="dz-row">
        <button className="dz-primary" disabled={!/^[a-z][a-z0-9_]{1,40}$/.test(newId) || newId.startsWith(SAMPLE_PREFIX)}
          onClick={() => {
            setSaving(null);
            void download(setIn(definition, ["character_id"], newId));
          }}>この ID で保存</button>
        <button onClick={() => setSaving(null)}>やめる</button>
      </div>
    </div>
  );

  if (page === "simulate") return <>{toolbar}{saveAsk}<Simulation api={api} definition={definition} canRun={!!check?.ok} /></>;
  if (page === "compare") return <>{toolbar}{saveAsk}<Compare api={api} choices={choices} definition={definition} baseline={draft.baseline} current={check} /></>;
  if (page === "types") return <>{toolbar}<Types api={api} choices={choices} /></>;

  const sectionProps = { def: definition, edit, choices, api };
  return (
    <>
      {toolbar}
      {saveAsk}
      {message && <p role="status" className="dz-message">{message}</p>}
      {mode === "designer" ? (
        <div className="dz-layout">
          <nav className="dz-sections" aria-label="設定の項目">
            {SECTIONS.map((s) => (
              <button key={s.id} className={section === s.id ? "active" : undefined} aria-current={section === s.id ? "true" : undefined} onClick={() => setSection(s.id)}>
                <span>{s.label}</span><span className="dz-hint">{s.hint}</span>
              </button>
            ))}
          </nav>
          <div className="dz-editor">
            <h2>{SECTIONS.find((s) => s.id === section)?.label}</h2>
            <Section id={section} {...sectionProps} />
          </div>
          <Preview check={check} busy={checking} error={checkError} />
        </div>
      ) : (
        <Wizard step={step} setStep={setStep} sectionProps={sectionProps} check={check} checking={checking} checkError={checkError}
          onSave={() => (usesSample ? setSaving("ask") : void download(definition))} api={api} definition={definition} />
      )}
    </>
  );
}

const WIZARD_STEPS: SectionId[] = ["basic", "type", "expression", "temperament", "likes", "events", "persona"];

function Wizard({ step, setStep, sectionProps, check, checking, checkError, onSave, api, definition }: {
  step: number; setStep: (n: number) => void; sectionProps: SectionProps;
  check: CheckResult | null; checking: boolean; checkError: string; onSave: () => void; api: DesignerApi; definition: Definition;
}) {
  const total = WIZARD_STEPS.length + 1; // 最後は「確認」
  const isLast = step >= WIZARD_STEPS.length;
  const id = WIZARD_STEPS[Math.min(step, WIZARD_STEPS.length - 1)];
  const label = isLast ? "確認" : SECTIONS.find((s) => s.id === id)?.label;
  return (
    <div className="dz-wizard">
      <ol className="dz-steps" aria-label="手順">
        {[...WIZARD_STEPS.map((s) => SECTIONS.find((x) => x.id === s)?.label ?? s), "確認"].map((name, i) => (
          <li key={name} className={i === step ? "active" : i < step ? "done" : undefined} aria-current={i === step ? "step" : undefined}>
            <button onClick={() => setStep(i)}>{i + 1}. {name}</button>
          </li>
        ))}
      </ol>
      <h2>{step + 1} / {total}: {label}</h2>
      {isLast ? (
        <div className="dz-review">
          <p>検査の結果を確かめ、模擬のシミュレーションで動きを見てから、YAML を保存してください。</p>
          <Simulation api={api} definition={definition} canRun={!!check?.ok} />
          <button className="dz-primary" onClick={onSave}>YAML を保存</button>
        </div>
      ) : (
        <Section id={id} {...sectionProps} />
      )}
      <div className="dz-wizard-nav">
        <button disabled={step === 0} onClick={() => setStep(step - 1)}>戻る</button>
        <button className="dz-primary" disabled={isLast} onClick={() => setStep(step + 1)}>次へ</button>
      </div>
      <Preview check={check} busy={checking} error={checkError} />
    </div>
  );
}
