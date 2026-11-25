// 診断(方式B。ペルソナデザイナ仕様書 §7.2)。想定するキャラクタについて答え、性格タイプを決める。
// 採点はサーバー(CLI と同じ処理)。回答はこの画面の中だけに持ち、採用したときだけ定義に入れる。

import { useEffect, useState } from "react";
import type { Definition, DesignerApi, Quiz, QuizResult } from "./api";

const SCALE = [
  [1, "まったく当てはまらない"],
  [2, "あまり当てはまらない"],
  [3, "どちらとも言えない"],
  [4, "やや当てはまる"],
  [5, "とても当てはまる"],
] as const;

export function QuizPanel({ api, onAdopt }: { api: DesignerApi; onAdopt: (basePersonality: Definition) => void }) {
  const [quiz, setQuiz] = useState<Quiz | null>(null);
  const [answers, setAnswers] = useState<Record<string, number>>({});
  const [tiebreak, setTiebreak] = useState<Record<string, string>>({});
  const [result, setResult] = useState<QuizResult | null>(null);
  const [message, setMessage] = useState("");

  useEffect(() => {
    api.quiz().then(setQuiz, (e) => setMessage((e as Error).message));
  }, [api]);

  async function score(nextAnswers = answers, nextTiebreak = tiebreak) {
    setMessage("");
    try {
      setResult(await api.scoreQuiz(nextAnswers, nextTiebreak));
    } catch (e) {
      setMessage((e as Error).message);
    }
  }

  if (!quiz) return <p aria-busy="true">{message || "読み込んでいます…"}</p>;
  const borderline = result ? Object.values(result.axes).filter((a) => a.borderline && !a.used_tiebreak).map((a) => a.axis) : [];
  const extra = borderline.flatMap((axis) => quiz.tiebreak[axis] ?? []);
  const answered = quiz.main.filter((q) => answers[q.id]).length;

  const questionRow = (q: { id: string; text: string }) => (
    <fieldset key={q.id} className="dz-question">
      <legend>このキャラクタは、{q.text}</legend>
      <div className="dz-scale">
        {SCALE.map(([value, label]) => (
          <label key={value} title={label}>
            <input type="radio" name={`quiz-${q.id}`} checked={answers[q.id] === value} onChange={() => setAnswers({ ...answers, [q.id]: value })} />
            <span>{value}</span>
          </label>
        ))}
      </div>
    </fieldset>
  );

  return (
    <div className="dz-quiz">
      <p className="dz-hint">{quiz.preamble}</p>
      <p className="dz-hint">1 まったく当てはまらない ・ 2 あまり ・ 3 どちらとも ・ 4 やや ・ 5 とても当てはまる(答えにくい質問は飛ばせます)</p>
      {quiz.main.map(questionRow)}
      <button className="dz-primary" onClick={() => void score()}>採点する({answered} / {quiz.main.length} 問)</button>
      {extra.length > 0 && (
        <>
          <p className="dz-hint">境界に近い軸があります。予備の質問にも答えて、もう一度採点してください。</p>
          {extra.map(questionRow)}
        </>
      )}
      {result && (
        <div className="dz-result" role="status">
          {Object.values(result.axes).map((a) => (
            <p key={a.axis}>
              {AXIS_NAMES[a.axis] ?? a.axis}: {a.score === null ? `回答が足りません(${a.answered}問。4問以上が必要)` : `${a.score.toFixed(2)}${a.borderline ? "(境界)" : ""}`}
            </p>
          ))}
          {result.undecided_axes.map((axis) => (
            <div key={axis} className="dz-row">
              <span>{AXIS_NAMES[axis] ?? axis}は境界です。どちらにしますか:</span>
              {(result.axes[axis]?.score ?? null) !== null && ["low", "high"].map((side) => {
                const letter = side === "low" ? lowLetter(axis) : highLetter(axis);
                return (
                  <button key={side} onClick={() => {
                    const next = { ...tiebreak, [axis]: letter };
                    setTiebreak(next);
                    void score(answers, next);
                  }}>{POLE_NAMES[letter] ?? letter}</button>
                );
              })}
            </div>
          ))}
          {result.warnings.map((w) => <p key={w} className="dz-warning">{w}</p>)}
          {result.type_id && result.base_personality && (
            <>
              <p><strong>{result.type_id} {result.type_name}</strong></p>
              <div className="dz-row">
                <button className="dz-primary" onClick={() => onAdopt({ ...result.base_personality!.preset, quiz: { ...(result.base_personality!.preset.quiz as object), completed_at: new Date().toISOString() } })}>
                  プリセットの値で採用
                </button>
                <button onClick={() => onAdopt({ ...result.base_personality!.scores, quiz: { ...(result.base_personality!.scores.quiz as object), completed_at: new Date().toISOString() } })}>
                  回答のスコアのまま採用
                </button>
              </div>
            </>
          )}
          <p className="dz-hint">{quiz.disclaimer}</p>
        </div>
      )}
      {message && <p role="alert" className="dz-error">{message}</p>}
    </div>
  );
}

// 軸の極の文字(0 に近い極 / 1 に近い極)。性格タイプの ID の各文字(例: oclp)
const POLES: Record<string, [string, string]> = { i: ["o", "i"], a: ["c", "a"], e: ["l", "e"], f: ["p", "f"] };
const POLE_NAMES: Record<string, string> = { o: "外向", i: "内向", c: "具体", a: "抽象", l: "論理", e: "共感", p: "計画", f: "柔軟" };
const AXIS_NAMES: Record<string, string> = { i: "内向度", a: "抽象度", e: "共感度", f: "柔軟度" };
function lowLetter(axis: string): string {
  return POLES[axis]?.[0] ?? "";
}
function highLetter(axis: string): string {
  return POLES[axis]?.[1] ?? "";
}
