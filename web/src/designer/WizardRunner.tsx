// ウィザード(本体仕様書 §15.1 の M10-2): 質問に1問ずつ答えるだけで、キャラクタができあがる。
// 質問の並び・確認の質問・組み立ては、サーバー(POST /v1/designer/wizard。状態を持たない)が決める。
// 画面は、答えた順の回答だけを持ち、タブの一時保存領域に残す(再読み込みしても続きから)。

import { useEffect, useRef, useState } from "react";
import type { Choices, Definition, DesignerApi, WizardAnswer, WizardQuestion, WizardStep } from "./api";
import type { WizardState } from "./draft";
import { Modal } from "./Modal";
import { Preview } from "./Preview";
import { Simulation } from "./Simulation";
import { Toolbar } from "./Toolbar";

const SCALE_LABELS = ["まったく当てはまらない", "あまり当てはまらない", "どちらともいえない", "やや当てはまる", "とても当てはまる"];

export function WizardRunner({ api, choices, state, onChange, onSave, onOpenDesigner, onRestart }: {
  api: DesignerApi; choices: Choices; state: WizardState; onChange: (s: WizardState) => void;
  onSave: (d: Definition) => void; onOpenDesigner: (d: Definition, source: string, jumpTo?: string) => void; onRestart: () => void;
}) {
  const [step, setStep] = useState<WizardStep | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [previous, setPrevious] = useState<WizardAnswer | undefined>(undefined); // 「戻る」で取り消した回答(選び直しの目印)
  // いまの質問で選ばれている回答(右下の「次へ」で送る)。戻ってきたときは前の回答、初めてなら既定の回答
  const [selected, setSelected] = useState<WizardAnswer | undefined>(undefined);
  // デザイナへの切り替えの確認(jumpTo: できあがりの画面で、検査の結果を押したときの移る先)
  const [confirming, setConfirming] = useState<{ jumpTo?: string } | null>(null);
  const confirmButton = useRef<HTMLButtonElement>(null);
  const [restartAsk, setRestartAsk] = useState(false); // できあがりの画面の「最初からやり直す」の確認
  useEffect(() => {
    // 確認のボタンに焦点を当てる(改行で、そのまま切り替えられる)
    confirmButton.current?.focus();
  }, [confirming]);
  const answers = Object.fromEntries(state.answers);

  // 回答が変わるたびに、次の質問を聞く
  useEffect(() => {
    let cancelled = false;
    setBusy(true);
    api.wizard({ character_id: state.character_id, name: state.name, answers: Object.fromEntries(state.answers) }).then(
      (r) => {
        if (cancelled) return;
        setStep(r);
        setError("");
        setBusy(false);
      },
      (e) => {
        if (cancelled) return;
        setError((e as Error).message);
        setBusy(false);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [api, state]);

  const answer = (id: string, value: WizardAnswer) => {
    setPrevious(undefined);
    onChange({ ...state, answers: [...state.answers, [id, value]] });
  };
  const back = () => {
    const last = state.answers[state.answers.length - 1];
    if (!last) return;
    setPrevious(last[1]);
    onChange({ ...state, answers: state.answers.slice(0, -1) });
  };

  // 残りを既定の回答にして組み立て、デザイナ(詳細設定)へ
  async function switchToDesigner() {
    const jumpTo = confirming?.jumpTo;
    setConfirming(null);
    if (step?.done && step.definition) {
      onOpenDesigner(step.definition, "ウィザード", jumpTo);
      return;
    }
    setBusy(true);
    try {
      const r = await api.wizard({ character_id: state.character_id, name: state.name, answers, finish: true });
      if (r.definition) onOpenDesigner(r.definition, "ウィザード");
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }

  const remaining = step ? step.progress.total - step.progress.answered : 0;
  const current = step?.question ?? null;
  const sectionIndex = current ? step!.sections.findIndex((s) => s.id === current.section) : step?.sections.length ?? 0;

  return (
    <div className="dz-wizard" aria-busy={busy}>
      <Toolbar name={state.name} id={state.character_id}>
        {!step?.done && (
          <button onClick={() => setConfirming({})} disabled={busy}>詳細設定(デザイナ)に切り替え</button>
        )}
        {/* できあがりの画面では、下の「最初からやり直す」と同じなので出さない */}
        {!step?.done && <button onClick={onRestart}>新しく始める</button>}
      </Toolbar>
      {confirming && (
        <Modal labelledBy="dz-switch-title" onCancel={() => setConfirming(null)}>
          <h3 id="dz-switch-title">デザイナに切り替えますか</h3>
          {step?.done ? (
            <p>デザイナに切り替えると、ウィザードの回答には戻れません(デザイナからウィザードに戻すと、IDと名前以外は消えます)。デザイナでは、1つ1つの設定を自分で調整できます。</p>
          ) : (
            <p>残りの {remaining} 問は、既定の回答(「どちらともいえない」など)をしたものとして、キャラクタを組み立てます。デザイナでは、1つ1つの設定を自分で調整できます。</p>
          )}
          <div className="dz-row">
            <button ref={confirmButton} className="dz-primary" onClick={() => void switchToDesigner()}>
              {step?.done ? "デザイナに切り替える" : "既定の回答で組み立てて、切り替える"}
            </button>
            <button onClick={() => setConfirming(null)}>{step?.done ? "やめる(この画面に戻る)" : "やめる(質問を続ける)"}</button>
          </div>
        </Modal>
      )}
      {restartAsk && (
        <Modal labelledBy="dz-restart-title" onCancel={() => setRestartAsk(false)}>
          <h3 id="dz-restart-title">最初からやり直しますか</h3>
          <p>すべての回答を既定値に戻して、最初の質問からやり直します。よろしいですか</p>
          <div className="dz-row">
            {/* はい: 回答をすべて消し(ID と名前は残す)、1問目からの状態として残してから、最初の画面へ */}
            <button className="dz-primary" onClick={() => {
              setRestartAsk(false);
              onChange({ ...state, answers: [] });
              onRestart();
            }}>はい</button>
            <button onClick={() => setRestartAsk(false)}>いいえ</button>
          </div>
        </Modal>
      )}
      {step && (
        <ol className="dz-steps" aria-label="質問の区切り">
          {step.sections.map((s, i) => (
            <li key={s.id} className={i === sectionIndex ? "active" : i < sectionIndex ? "done" : undefined} aria-current={i === sectionIndex ? "step" : undefined}>
              <span>{s.label}</span>
            </li>
          ))}
        </ol>
      )}
      {error && <p role="alert" className="dz-error">{error}</p>}
      {step && current && (
        <>
          <p className="dz-progress">
            質問 {step.progress.answered + 1} / {step.progress.total}
            <progress max={step.progress.total} value={step.progress.answered} aria-label="答えた質問の数" />
          </p>
          <QuestionView key={`${current.id}-${state.answers.length}`} question={current} choices={choices} previous={previous}
            liked={answers.lk_liked as Record<string, string | null> | undefined} busy={busy} onAnswer={(v) => answer(current.id, v)}
            onSelect={setSelected} />
          <div className="dz-wizard-nav">
            <button onClick={back} disabled={busy || state.answers.length === 0}>戻る</button>
            <button className="dz-primary" disabled={busy || selected === undefined}
              onClick={() => selected !== undefined && answer(current.id, compact(selected))}>次へ</button>
          </div>
        </>
      )}
      {step?.done && step.definition && (
        <div className="dz-review">
          <h2>できあがりました</h2>
          <p>
            {state.name}の設定を、回答から組み立てました。
            {step.defaulted ? `(${step.defaulted} 問は既定の回答にしました)` : ""}
            YAML を保存するか、デザイナで細かく調整してください。検査の結果を押すと、デザイナのその項目に移ります。人格の説明や話し方に書き足したいことがあれば、デザイナの「人格・話し方」で書けます。
          </p>
          {step.acknowledged && step.acknowledged.length > 0 && (
            <p className="dz-hint">確認の質問で確かめた警告: {step.acknowledged.join("、")}</p>
          )}
          <div className="dz-row">
            <button className="dz-primary" onClick={() => onSave(step.definition!)}>YAML を保存</button>
            <button onClick={() => setConfirming({})}>デザイナで細かく調整する</button>
            <button onClick={back}>最後の質問に戻る</button>
            <button onClick={() => setRestartAsk(true)}>最初からやり直す</button>
          </div>
          <Simulation api={api} definition={step.definition} canRun={!!step.check?.ok} />
          <Preview check={step.check ?? null} busy={false} error="" onJump={(path) => setConfirming({ jumpTo: path })} />
        </div>
      )}
    </div>
  );
}

/** 好き嫌いの回答から、「選ばない」の区分を除く。 */
function compact(value: WizardAnswer): WizardAnswer {
  if (value === null || typeof value !== "object") return value;
  return Object.fromEntries(Object.entries(value).filter(([, v]) => v));
}

function QuestionView({ question, choices, previous, liked, busy, onAnswer, onSelect }: {
  question: WizardQuestion; choices: Choices; previous: WizardAnswer | undefined; liked: Record<string, string | null> | undefined;
  busy: boolean; onAnswer: (v: WizardAnswer) => void; onSelect: (v: WizardAnswer) => void;
}) {
  const initial = previous ?? question.default;
  const focus = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    focus.current?.focus();
    onSelect(initial); // 右下の「次へ」は、この回答で進む
    // 質問ごとに1回だけ(質問が変わると、作り直される)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (question.kind === "scale" || question.kind === "choice") {
    const options = question.kind === "scale"
      ? SCALE_LABELS.map((label, i) => ({ value: i + 1, label }))
      : question.options.map((o) => ({ value: o.value as WizardAnswer, label: o.label }));
    return (
      <fieldset className="dz-ask">
        <legend>{question.text}</legend>
        {question.note && <p className="dz-hint">{question.note}</p>}
        <div className={question.kind === "scale" ? "dz-ask-scale" : "dz-ask-choices"}>
          {options.map((o) => (
            <button key={String(o.value)} ref={o.value === initial ? focus : undefined} disabled={busy}
              aria-pressed={o.value === initial} onClick={() => onAnswer(o.value)}>
              {o.label}
            </button>
          ))}
        </div>
      </fieldset>
    );
  }
  return <SheetQuestion question={question} choices={choices} initial={(initial ?? {}) as Record<string, string | null>} liked={liked} busy={busy} onSelect={onSelect} />;
}

/** 好き嫌い: 区分ごとに1つまで(選ばない区分があってもよい)。苦手には、好きに選んだものを出さない。 */
function SheetQuestion({ question, choices, initial, liked, busy, onSelect }: {
  question: WizardQuestion; choices: Choices; initial: Record<string, string | null>; liked: Record<string, string | null> | undefined;
  busy: boolean; onSelect: (v: WizardAnswer) => void;
}) {
  const [picked, setPickedState] = useState<Record<string, string | null>>(initial);
  const setPicked = (next: Record<string, string | null>) => {
    setPickedState(next);
    onSelect(next); // 右下の「次へ」で送る
  };
  const dislikes = question.kind === "dislikes";
  const taken = new Set(Object.values(liked ?? {}).filter(Boolean));
  return (
    <fieldset className="dz-ask">
      <legend>{question.text}</legend>
      {question.note && <p className="dz-hint">{question.note}</p>}
      <div className="dz-ask-sheet">
        {choices.likes_catalog.categories.map((c) => (
          <label key={c.id} className="dz-field">
            <span>{c.label}</span>
            <select value={picked[c.id] ?? ""} disabled={busy} onChange={(e) => setPicked({ ...picked, [c.id]: e.target.value || null })}>
              <option value="">選ばない</option>
              {choices.likes_catalog.items
                .filter((i) => i.category === c.id && (dislikes ? i.dislikeable && !taken.has(i.id) : i.likeable))
                .map((i) => <option key={i.id} value={i.id}>{i.label}</option>)}
            </select>
          </label>
        ))}
      </div>
    </fieldset>
  );
}
