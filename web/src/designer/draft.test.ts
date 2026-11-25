import { describe, expect, it } from "vitest";
import { diffDefinitions, fileName, getIn, setIn } from "./draft";

describe("下書きの操作", () => {
  it("path の値を、元を変えずに変える(なければ作る。undefined で消す)", () => {
    const before = { profile: { deltas: { talkativeness: 0.1 } }, list: [{ a: 1 }] };
    const after = setIn(before, ["profile", "deltas", "initiative"], 0.2);
    expect(after).toEqual({ profile: { deltas: { talkativeness: 0.1, initiative: 0.2 } }, list: [{ a: 1 }] });
    expect(before.profile.deltas).toEqual({ talkativeness: 0.1 });
    expect(setIn(after, ["profile", "deltas", "talkativeness"], undefined)).toEqual({ profile: { deltas: { initiative: 0.2 } }, list: [{ a: 1 }] });
    expect(setIn(before, ["list", 0, "a"], 2).list).toEqual([{ a: 2 }]);
    expect(getIn(after, ["profile", "deltas", "initiative"])).toBe(0.2);
    expect(getIn(after, ["missing", "x"])).toBeUndefined();
  });

  it("変わった項目だけを、path つきで並べる", () => {
    expect(diffDefinitions({ a: 1, b: { c: [1] }, d: "x" }, { a: 1, b: { c: [2] }, e: true })).toEqual([
      { path: "b.c", before: [1], after: [2] },
      { path: "d", before: "x", after: undefined },
      { path: "e", before: undefined, after: true },
    ]);
  });

  it("ファイル名は ID から作る(使えない文字は _ に)", () => {
    expect(fileName({ character_id: "my_char" })).toBe("my_char.persona.yaml");
    expect(fileName({ character_id: "../x" })).toBe("___x.persona.yaml");
    expect(fileName({})).toBe("persona.persona.yaml");
  });
});
