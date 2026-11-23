// 同意の文言の、Markdown のリンクの表示のテスト。
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ConsentLabel } from "./consentLabel";

const html = (text: string) => renderToStaticMarkup(<ConsentLabel text={text} />);

describe("ConsentLabel", () => {
  it("https のリンクは、新しいタブで開くリンクにする", () => {
    expect(html("[利用規約](https://example.com/terms)に同意します。")).toBe(
      '<a href="https://example.com/terms" target="_blank" rel="noopener noreferrer">利用規約</a>に同意します。',
    );
  });

  it("仮の URL や https 以外は、文字だけを表示する", () => {
    expect(html("[利用規約](【URL】)に同意します。")).toBe("利用規約に同意します。");
    expect(html("[x](javascript:alert(1))")).not.toContain("<a");
  });

  it("リンクがない文言は、そのまま表示する", () => {
    expect(html("記憶の保存に同意します。")).toBe("記憶の保存に同意します。");
  });
});
