// 同意と年齢の画面(本体仕様書 §13.4.4、legal/consent.md。§15.1 の M7)。
// 画面と文面は API(GET /v1/account/consents)が、利用者の年齢に合わせて返す。必須の項目に、あらかじめチェックを入れない。

import { useEffect, useState } from "react";
import type { Api, ConsentStatus } from "./api";
import { ConsentLabel } from "./consentLabel";

type Props = { api: Api; onDone: () => void };

export function Consent({ api, onDone }: Props) {
  const [status, setStatus] = useState<ConsentStatus | null>(null);
  const [index, setIndex] = useState(0);
  const [checked, setChecked] = useState<Record<string, boolean>>({});
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api.consentStatus().then(setStatus, (e: Error) => setError(e.message));
  }, [api]);

  if (error) return <p role="alert">{error}</p>;
  if (!status) return <p aria-busy="true">読み込んでいます…</p>;
  const screen = status.screens[index];
  if (!screen) return null;

  const isAge = screen.items.length === 0; // 年齢の質問(同意ではない)
  const requiredOk = screen.items.every((item) => !item.required || item.granted || checked[item.type]);

  async function next(adult?: boolean | null) {
    setSaving(true);
    setError("");
    try {
      let latest = status as ConsentStatus;
      if (isAge) {
        await api.declareAge(adult ?? null);
        latest = await api.consentStatus(); // 年齢で、表示する画面が変わる(保護者の同意、任意の設定)
      } else {
        const types = screen.items.filter((item) => checked[item.type] && !item.granted).map((item) => item.type);
        if (types.length > 0) latest = await api.grantConsents(types);
      }
      const position = latest.screens.findIndex((s) => s.id === screen.id);
      if (position + 1 >= latest.screens.length) {
        if (latest.missing.length === 0) onDone();
        else setError("必要な同意が、まだそろっていません。前の画面に戻って、ご確認ください。");
      } else {
        setStatus(latest);
        setIndex(position + 1);
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="consent" aria-labelledby="consent-title">
      <p className="consent-step">
        {index + 1} / {status.screens.length}(同意書 版 {status.version})
      </p>
      <h1 id="consent-title">{screen.title}</h1>
      <div className="consent-text">
        {screen.text.split("\n").map((line, i) => (
          <p key={i}>{line}</p>
        ))}
      </div>
      {isAge ? (
        <div className="choices" role="group" aria-label="18歳以上ですか">
          <button disabled={saving} onClick={() => next(true)}>はい</button>
          <button disabled={saving} onClick={() => next(false)}>いいえ</button>
          <button disabled={saving} onClick={() => next(null)}>答えない</button>
        </div>
      ) : (
        <>
          <ul className="consent-items">
            {screen.items.map((item) => (
              <li key={item.type}>
                <label>
                  <input
                    type="checkbox"
                    checked={item.granted || !!checked[item.type]}
                    disabled={item.granted}
                    onChange={(e) => setChecked({ ...checked, [item.type]: e.target.checked })}
                  />{" "}
                  <ConsentLabel text={item.label} />
                  {item.required ? "(必須)" : "(任意)"}
                </label>
              </li>
            ))}
          </ul>
          <div className="choices">
            {index > 0 && (
              <button disabled={saving} onClick={() => setIndex(index - 1)}>
                戻る
              </button>
            )}
            <button disabled={saving || !requiredOk} onClick={() => next()}>
              次へ
            </button>
          </div>
        </>
      )}
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
