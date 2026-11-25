// デザイナの設定の項目(セクションごと)。1画面の「デザイナ」と「ウィザード」で、同じ部品を使う。
// 値の範囲は、ペルソナデザイナ仕様書 §4.3。検査はサーバーの lint(プレビューに出る)。

import type { ReactNode } from "react";
import { getIn, SAMPLE_PREFIX } from "./draft";
import { Check, Select, Slider, TextField } from "./fields";
import {
  ADJUSTABLE_LABELS, ASSOCIATION_LABELS, CONCEPT_FIELDS, DELTA_KEYS, EMOTION_LABELS, FIXED_LABELS, num, REPEAT_LABELS,
  signed, str, STRENGTH_LABELS, TRAIT_KEYS, TRAIT_LABELS, VAD_LABELS,
} from "./labels";
import { QuizPanel } from "./Quiz";

import type { Edit, SectionId, SectionProps } from "./sectionList";

export type { Edit, SectionId, SectionProps };
export { SECTIONS } from "./sectionList";

export function Section({ id, ...props }: SectionProps & { id: SectionId }) {
  switch (id) {
    case "basic": return <BasicSection {...props} />;
    case "type": return <TypeSection {...props} />;
    case "expression": return <ExpressionSection {...props} />;
    case "temperament": return <TemperamentSection {...props} />;
    case "likes": return <LikesSection {...props} />;
    case "events": return <EventsSection {...props} />;
    case "topics": return <TopicsSection {...props} />;
    case "persona": return <PersonaSection {...props} />;
  }
}

function Group({ title, children, hint }: { title: string; children: ReactNode; hint?: string }) {
  return (
    <fieldset className="dz-group">
      <legend>{title}</legend>
      {hint && <p className="dz-hint">{hint}</p>}
      {children}
    </fieldset>
  );
}

// --- 基本 ------------------------------------------------------------------------------------

function BasicSection({ def, edit }: SectionProps) {
  const id = str(def.character_id);
  return (
    <>
      <Group title="キャラクタ">
        <TextField label="キャラクタの ID(英数字と _)" value={id} maxLength={40} onChange={(v) => edit(["character_id"], v)}
          hint={id.startsWith(SAMPLE_PREFIX) ? "サンプルの ID です。保存するときは、自分の ID に変えてください。" : undefined} />
        <TextField label="名前" value={str(def.name)} maxLength={40} onChange={(v) => edit(["name"], v)} />
        <TextField label="版(X.Y.Z)" value={str(def.version)} maxLength={20} onChange={(v) => edit(["version"], v)} />
      </Group>
      <Group title="コンセプト" hint="設計のメモです(キャラクタの動きには使いません)。">
        {CONCEPT_FIELDS.map(([key, label]) => (
          <TextField key={key} label={label} value={str(getIn(def, ["concept", key]))} maxLength={200} onChange={(v) => edit(["concept", key], v)} />
        ))}
      </Group>
    </>
  );
}

// --- 性格タイプ ------------------------------------------------------------------------------

function TypeSection({ def, edit, choices, api }: SectionProps) {
  const base = (def.base_personality ?? {}) as Record<string, unknown>;
  const source = str(base.source) || "preset";
  const typeId = str(base.type);
  const current = choices.types.find((t) => t.type_id === typeId);
  const setPreset = (id: string) => {
    const t = choices.types.find((x) => x.type_id === id);
    if (t) edit(["base_personality"], { type: id, axes: { ...t.axes }, source: "preset", quiz: null });
  };
  return (
    <>
      <Group title="性格タイプの決め方" hint="性格タイプは、キャラクタの設計のための分類です(人の性格の診断ではありません)。">
        <div className="dz-choices" role="radiogroup" aria-label="性格タイプの決め方">
          {[["preset", "プリセットから選ぶ"], ["quiz", "質問に答えて決める"], ["custom", "軸を自分で決める"]].map(([value, label]) => (
            <label key={value} className="dz-radio">
              <input type="radio" name="type-source" checked={source === value}
                onChange={() => edit(["base_personality", "source"], value)} />
              {label}
            </label>
          ))}
        </div>
      </Group>
      {source === "preset" && (
        <Group title="プリセット">
          <Select label="性格タイプ" value={typeId}
            options={[{ value: "", label: "(選んでください)" }, ...choices.types.map((t) => ({ value: t.type_id, label: `${t.type_id} ${t.name}(${t.composition})` }))]}
            onChange={(v) => v && setPreset(v)} />
          {current && <p className="dz-card">{current.style_card}</p>}
        </Group>
      )}
      {source === "quiz" && (
        <Group title="診断" hint="想定するキャラクタについて答えてください(あなた自身についてではありません)。">
          <QuizPanel api={api} onAdopt={(value) => edit(["base_personality"], value)} />
        </Group>
      )}
      {(source === "custom" || source === "quiz") && (
        <Group title="軸" hint={source === "quiz" ? "診断の結果から、細かく調整できます。" : "0.5 ちょうどの軸があると、タイプは決まりません。"}>
          {choices.axes.map((axis) => (
            <Slider key={axis.id} label={`${axis.name}(${axis.low} ← → ${axis.high})`} min={0} max={1} step={0.01}
              value={num(getIn(def, ["base_personality", "axes", axis.id]), 0.5)}
              onChange={(v) => edit(["base_personality", "axes", axis.id], v)} />
          ))}
          <Select label="タイプ(軸の側と合わせる)" value={typeId}
            options={[{ value: "", label: "(なし)" }, ...choices.types.map((t) => ({ value: t.type_id, label: `${t.type_id} ${t.name}` }))]}
            onChange={(v) => edit(["base_personality", "type"], v || null)} />
        </Group>
      )}
    </>
  );
}

// --- 表出 ------------------------------------------------------------------------------------

function ExpressionSection({ def, edit, choices }: SectionProps) {
  const preset = str(getIn(def, ["profile", "preset"]));
  const overrides = (def.overrides ?? {}) as Record<string, number>;
  const adjustable = (getIn(def, ["adaptation", "adjustable"]) as string[] | undefined) ?? [];
  const fixed = (getIn(def, ["adaptation", "fixed"]) as string[] | undefined) ?? [];
  const toggle = (list: string[], key: string, on: boolean) => (on ? [...new Set([...list, key])] : list.filter((k) => k !== key));
  return (
    <>
      <Group title="表出プロファイル" hint="プリセットを選ぶと、差分がプリセットの値になります(あとで調整できます)。">
        <Select label="プリセット" value={preset}
          options={[{ value: "", label: "なし" }, ...choices.profiles.map((p) => ({ value: p.id, label: p.name }))]}
          onChange={(v) => {
            const p = choices.profiles.find((x) => x.id === v);
            edit(["profile"], { preset: v || null, deltas: p ? { ...p.deltas } : {} });
          }} />
      </Group>
      <Group title="性格タイプの既定値への差分" hint="-1〜+1。解決のときに 0〜1 に収めます。">
        {DELTA_KEYS.map((key) => (
          <Slider key={key} label={TRAIT_LABELS[key]} min={-1} max={1} step={0.05} format={(v) => signed(v)}
            value={num(getIn(def, ["profile", "deltas", key]))} onChange={(v) => edit(["profile", "deltas", key], v === 0 ? undefined : v)} />
        ))}
        {Object.entries(ASSOCIATION_LABELS).map(([key, label]) => (
          <Slider key={key} label={label} min={-1} max={1} step={0.05} format={(v) => signed(v)}
            value={num(getIn(def, ["profile", "deltas", key]))} onChange={(v) => edit(["profile", "deltas", key], v === 0 ? undefined : v)} />
        ))}
      </Group>
      <Group title="上書き(任意)" hint="解決済みの値を、直接決めます。ふつうは使いません。">
        {TRAIT_KEYS.map((key) => (
          <div key={key} className="dz-override">
            <Check label={`${TRAIT_LABELS[key]}を上書きする`} checked={key in overrides}
              onChange={(on) => edit(["overrides", key], on ? 0.5 : undefined)} />
            {key in overrides && (
              <Slider label={TRAIT_LABELS[key]} min={0} max={1} step={0.05} value={num(overrides[key])} onChange={(v) => edit(["overrides", key], v)} />
            )}
          </div>
        ))}
      </Group>
      <Group title="関係と接し方の調整">
        <Slider label="関係値の初期値(-20〜+20)" min={-20} max={20} step={1} value={num(def.initial_relationship)}
          onChange={(v) => edit(["initial_relationship"], v)} />
        <Slider label="調整のしやすさ" min={0} max={1} step={0.05} value={num(getIn(def, ["adaptation", "adaptability"]), 0.5)}
          onChange={(v) => edit(["adaptation", "adaptability"], v)} />
        <p className="dz-hint">ユーザーの「優しくして」などの要望で、調整してよいもの:</p>
        <div className="dz-checks">
          {Object.entries(ADJUSTABLE_LABELS).map(([key, label]) => (
            <Check key={key} label={label} checked={adjustable.includes(key)} onChange={(on) => edit(["adaptation", "adjustable"], toggle(adjustable, key, on))} />
          ))}
        </div>
        <p className="dz-hint">変えないもの:</p>
        <div className="dz-checks">
          {Object.entries(FIXED_LABELS).map(([key, label]) => (
            <Check key={key} label={label} checked={fixed.includes(key)} onChange={(on) => edit(["adaptation", "fixed"], toggle(fixed, key, on))} />
          ))}
        </div>
      </Group>
    </>
  );
}

// --- 気質 ------------------------------------------------------------------------------------

function TemperamentSection({ def, edit }: SectionProps) {
  return (
    <>
      <Group title="感情の動きやすさ">
        <Slider label="反応の大きさ" min={0} max={1} step={0.05} value={num(getIn(def, ["temperament", "reactivity"]), 0.5)}
          onChange={(v) => edit(["temperament", "reactivity"], v)} />
        <Slider label="落ち着くまでの速さ" min={0} max={1} step={0.05} hint="推奨は 0.1〜0.6"
          value={num(getIn(def, ["temperament", "recovery_rate"]), 0.3)} onChange={(v) => edit(["temperament", "recovery_rate"], v)} />
      </Group>
      <Group title="ふだんの気分(-1〜+1)">
        {Object.entries(VAD_LABELS).map(([key, label]) => (
          <Slider key={key} label={label} min={-1} max={1} step={0.05} format={(v) => signed(v)}
            value={num(getIn(def, ["temperament", "baseline_vad", key]))} onChange={(v) => edit(["temperament", "baseline_vad", key], v)} />
        ))}
      </Group>
      <Group title="無言" hint="無言が続くのは2回まで(絶対の規則)。">
        <Select label="連続して無言になれる回数" value={String(num(getIn(def, ["silence_policy", "max_consecutive"]), 2))}
          options={[0, 1, 2].map((n) => ({ value: String(n), label: `${n}回` }))}
          onChange={(v) => edit(["silence_policy", "max_consecutive"], Number(v))} />
        <Check label="怒ったときに黙ってよい" checked={getIn(def, ["silence_policy", "allow_anger_silence"]) === true}
          onChange={(on) => edit(["silence_policy", "allow_anger_silence"], on)} />
      </Group>
    </>
  );
}

// --- 好き嫌い --------------------------------------------------------------------------------

const THING_CATEGORIES = ["color", "food", "event"];
const DISLIKE_EMOTIONS = ["fear", "sadness", "disgust", "anger", "surprise"];

function LikesSection({ def, edit, choices }: SectionProps) {
  const { categories, items } = choices.likes_catalog;
  const sheet = (def.likes_dislikes ?? null) as Record<string, unknown> | null;
  const base = sheet ?? { catalog_version: choices.likes_catalog.version, liked: {}, disliked: {} };
  const set = (side: "liked" | "disliked", category: string, value: unknown) => {
    const next = { ...base, [side]: { ...((base[side] as object) ?? {}), [category]: value } };
    edit(["likes_dislikes"], next);
  };
  return (
    <>
      <p className="dz-hint">区分ごとに1つ、好みカタログから選びます。好きなものは、うれしい文脈のときだけ喜びます。</p>
      {categories.map((c) => {
        const liked = getIn(base, ["liked", c.id]) as Record<string, unknown> | null | undefined;
        const disliked = getIn(base, ["disliked", c.id]) as Record<string, unknown> | null | undefined;
        const likedItem = items.find((i) => i.id === liked?.id);
        const thing = THING_CATEGORIES.includes(c.id);
        return (
          <Group key={c.id} title={c.label}>
            <div className="dz-row">
              <Select label="好き" value={str(liked?.id)}
                options={[{ value: "", label: "なし" }, ...items.filter((i) => i.category === c.id && i.likeable).map((i) => ({ value: i.id, label: i.label }))]}
                onChange={(v) => set("liked", c.id, v ? { id: v, strength: str(liked?.strength) || "medium" } : null)} />
              {liked && (
                <Select label="強さ" value={str(liked.strength) || "medium"}
                  options={Object.entries(STRENGTH_LABELS).map(([value, label]) => ({ value, label }))}
                  onChange={(v) => set("liked", c.id, { ...liked, strength: v })} />
              )}
              {liked && likedItem?.involvement && (
                <Select label="反応する場面" value={str(liked.scope) || "any"}
                  options={[{ value: "any", label: "いつでも" }, { value: "with_character", label: "ユーザーとキャラクタの間の行為のとき" }]}
                  onChange={(v) => set("liked", c.id, { ...liked, scope: v })} />
              )}
            </div>
            <div className="dz-row">
              <Select label="嫌い" value={str(disliked?.id)}
                options={[{ value: "", label: "なし" }, ...items.filter((i) => i.category === c.id && i.dislikeable).map((i) => ({ value: i.id, label: i.label }))]}
                onChange={(v) => set("disliked", c.id, v ? (thing ? { id: v, emotions: ["disgust"], strength: "medium", repeat: "habituate" } : { id: v, emotion: "disgust", strength: "medium", avoid: true }) : null)} />
              {disliked && (
                <Select label="強さ" value={str(disliked.strength) || "medium"}
                  options={Object.entries(STRENGTH_LABELS).map(([value, label]) => ({ value, label }))}
                  onChange={(v) => set("disliked", c.id, { ...disliked, strength: v })} />
              )}
              {disliked && thing && (
                <Select label="くり返したとき" value={str(disliked.repeat) || "habituate"}
                  options={Object.entries(REPEAT_LABELS).map(([value, label]) => ({ value, label }))}
                  onChange={(v) => set("disliked", c.id, { ...disliked, repeat: v })} />
              )}
              {disliked && !thing && (
                <>
                  <Select label="感情" value={str(disliked.emotion) || "disgust"}
                    options={["disgust", "fear"].map((e) => ({ value: e, label: EMOTION_LABELS[e] }))}
                    onChange={(v) => set("disliked", c.id, { ...disliked, emotion: v })} />
                  <Check label="話題を変えようとする" checked={disliked.avoid !== false} onChange={(on) => set("disliked", c.id, { ...disliked, avoid: on })} />
                </>
              )}
            </div>
            {disliked && thing && (
              <div className="dz-checks" aria-label="嫌いなときの感情">
                {DISLIKE_EMOTIONS.map((e) => {
                  const list = (disliked.emotions as string[] | undefined) ?? [];
                  return (
                    <Check key={e} label={EMOTION_LABELS[e]} checked={list.includes(e)}
                      onChange={(on) => set("disliked", c.id, { ...disliked, emotions: on ? [...list, e] : list.filter((x) => x !== e) })} />
                  );
                })}
              </div>
            )}
          </Group>
        );
      })}
    </>
  );
}

// --- 感情の効果(話題ルール・イベントルールで共通) ---------------------------------------------

function EffectsEditor({ value, onChange, emotions }: { value: Record<string, number>; onChange: (v: Record<string, number>) => void; emotions: string[] }) {
  return (
    <div className="dz-effects">
      {emotions.map((e) => (
        <Slider key={e} label={EMOTION_LABELS[e] ?? e} min={-1} max={1} step={0.1} format={(v) => signed(v, 1)} value={num(value[e])}
          onChange={(v) => {
            const next = { ...value };
            if (v === 0) delete next[e];
            else next[e] = v;
            onChange(next);
          }} />
      ))}
    </div>
  );
}

// --- イベント --------------------------------------------------------------------------------

function EventsSection({ def, edit, choices }: SectionProps) {
  const rules = (def.event_rules as Record<string, unknown>[] | undefined) ?? [];
  const add = (name: string) => {
    const trigger = choices.event_triggers.find((t) => t.name === name);
    if (!trigger) return;
    const id = `${name}_${rules.length + 1}`.replace(/[^a-z0-9_]/g, "_");
    edit(["event_rules"], [...rules, { id, trigger: { kind: trigger.kind, name }, effects: {}, gain: 0.2 }]);
  };
  return (
    <>
      <p className="dz-hint">ユーザーの発話(感謝・謝罪・称賛など)や、久しぶりの会話への、キャラクタの感情の反応です(30件まで)。</p>
      {rules.map((rule, index) => {
        const name = str(getIn(rule, ["trigger", "name"]));
        const trigger = choices.event_triggers.find((t) => t.name === name);
        const path = ["event_rules", index];
        return (
          <Group key={index} title={`${trigger?.label ?? name}(${str(rule.id)})`}>
            <TextField label="ルールの ID(英小文字・数字・_)" value={str(rule.id)} maxLength={41} onChange={(v) => edit([...path, "id"], v)} />
            {trigger?.positive_only && <p className="dz-hint">このきっかけには、うれしい方向の効果だけを付けられます。</p>}
            <EffectsEditor emotions={choices.emotions} value={(rule.effects as Record<string, number>) ?? {}} onChange={(v) => edit([...path, "effects"], v)} />
            <Slider label="強さ(gain)" min={0} max={1} step={0.05} value={num(rule.gain, 0.2)} onChange={(v) => edit([...path, "gain"], v)} />
            <Slider label="次に反応するまでのターン数" min={0} max={50} step={1} value={num(rule.cooldown_turns)} onChange={(v) => edit([...path, "cooldown_turns"], v || undefined)} />
            <Slider label="1回の会話での上限" min={1} max={20} step={1} value={num(rule.max_per_session, 5)} onChange={(v) => edit([...path, "max_per_session"], v)} />
            {name === "session_gap" && (
              <Slider label="何日ぶりから(3日以上)" min={1} max={90} step={1} value={num(getIn(rule, ["trigger", "min_days"]), 3)}
                onChange={(v) => edit([...path, "trigger", "min_days"], v)} />
            )}
            <button className="dz-danger" onClick={() => edit(["event_rules"], rules.filter((_, i) => i !== index))}>このルールを削除</button>
          </Group>
        );
      })}
      {rules.length < 30 && (
        <Select label="ルールを追加(きっかけを選ぶ)" value=""
          options={[{ value: "", label: "(選んでください)" }, ...choices.event_triggers.map((t) => ({ value: t.name, label: t.label }))]}
          onChange={add} />
      )}
    </>
  );
}

// --- 話題ルールと嗜好 ------------------------------------------------------------------------

function TopicsSection({ def, edit, choices }: SectionProps) {
  const rules = (def.appraisal_rules as Record<string, unknown>[] | undefined) ?? [];
  const prefs = (def.preferences as Record<string, unknown>[] | undefined) ?? [];
  return (
    <>
      <Group title="話題ルール" hint="好みカタログにない話題への反応です。話題が出たとき、キャラクタの感情がどう動くか。">
        {rules.map((rule, index) => (
          <div key={index} className="dz-item">
            <TextField label="話題" value={str(rule.topic)} maxLength={60} onChange={(v) => edit(["appraisal_rules", index, "topic"], v)} />
            <EffectsEditor emotions={choices.emotions} value={(rule.effects as Record<string, number>) ?? {}} onChange={(v) => edit(["appraisal_rules", index, "effects"], v)} />
            <Slider label="強さ(gain)" min={0} max={1} step={0.05} value={num(rule.gain, 0.5)} onChange={(v) => edit(["appraisal_rules", index, "gain"], v)} />
            <button className="dz-danger" onClick={() => edit(["appraisal_rules"], rules.filter((_, i) => i !== index))}>削除</button>
          </div>
        ))}
        <button onClick={() => edit(["appraisal_rules"], [...rules, { topic: "", match: "semantic", effects: { joy: 0.5 }, gain: 0.5 }])}>話題ルールを追加</button>
      </Group>
      <Group title="嗜好" hint="思い出しやすさに効く、話題の好み(-1 苦手 〜 +1 好き)。">
        {prefs.map((pref, index) => (
          <div key={index} className="dz-item dz-row">
            <TextField label="話題" value={str(pref.topic)} maxLength={60} onChange={(v) => edit(["preferences", index, "topic"], v)} />
            <Slider label="好み" min={-1} max={1} step={0.1} format={(v) => signed(v, 1)} value={num(pref.affinity)} onChange={(v) => edit(["preferences", index, "affinity"], v)} />
            <button className="dz-danger" onClick={() => edit(["preferences"], prefs.filter((_, i) => i !== index))}>削除</button>
          </div>
        ))}
        <button onClick={() => edit(["preferences"], [...prefs, { topic: "", affinity: 0.5 }])}>嗜好を追加</button>
      </Group>
    </>
  );
}

// --- 人格・話し方 ----------------------------------------------------------------------------

function PersonaSection({ def, edit }: SectionProps) {
  return (
    <Group title="人格と話し方" hint="恋愛的な関係・依存を誘う指示や、安全の規則を外す指示は書けません(検査で止まります)。">
      <TextField label="性格ディレクティブ" multiline maxLength={2000} value={str(def.persona_prompt)} onChange={(v) => edit(["persona_prompt"], v)} />
      <TextField label="話し方" multiline maxLength={1000} value={str(def.speech_style)} onChange={(v) => edit(["speech_style"], v)} />
    </Group>
  );
}
