// 検査の結果の場所から、設定の項目と、移る先の欄を決める(本体仕様書 §15.1 の M10-2 の手順3)。

import { describe, expect, it } from "vitest";
import { normalizePath, pathCandidates, sectionForPath } from "./sectionList";

describe("検査の結果の場所", () => {
  it("先頭の鍵で、設定の項目が決まる", () => {
    expect(sectionForPath("profile.deltas.talkativeness")).toBe("expression");
    expect(sectionForPath("adaptation")).toBe("expression");
    expect(sectionForPath("temperament.recovery_rate")).toBe("temperament");
    expect(sectionForPath("likes_dislikes.liked.color")).toBe("likes");
    expect(sectionForPath("event_rules[2]")).toBe("events");
    expect(sectionForPath("preferences")).toBe("topics");
    expect(sectionForPath("speech_style")).toBe("persona");
    expect(sectionForPath("base_personality.quiz.answers")).toBe("type");
    expect(sectionForPath("")).toBeNull();
    expect(sectionForPath("unknown")).toBeNull();
  });

  it("配列の番号の書き方をそろえ、詳しい場所から親へ順に探す", () => {
    expect(normalizePath("event_rules[2].gain")).toBe("event_rules.2.gain");
    expect(pathCandidates("event_rules[2].gain")).toEqual(["event_rules.2.gain", "event_rules.2", "event_rules"]);
    expect(pathCandidates("persona_prompt")).toEqual(["persona_prompt"]);
  });
});
