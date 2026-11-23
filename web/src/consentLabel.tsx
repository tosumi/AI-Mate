// 同意の項目の文言にある Markdown のリンク `[文字](URL)` を表示する。
// URL が https のときだけリンクにし、それ以外(公開前の仮の「【URL】」など)は文字だけを表示する。

import type { ReactNode } from "react";

const LINK = /\[([^\]]+)\]\(([^)]*)\)/g;

export function ConsentLabel({ text }: { text: string }) {
  const parts: ReactNode[] = [];
  let last = 0;
  for (const match of text.matchAll(LINK)) {
    const [whole, label, url] = match;
    const index = match.index ?? 0;
    if (index > last) parts.push(text.slice(last, index));
    parts.push(
      url.startsWith("https://") ? (
        <a key={index} href={url} target="_blank" rel="noopener noreferrer">
          {label}
        </a>
      ) : (
        label
      ),
    );
    last = index + whole.length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return <>{parts}</>;
}
