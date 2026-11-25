// 上部の帯(1行目 = 名前と ID、2行目 = 操作のボタン)。スクロールしても見えるように、ヘッダの下に固定する。
// ヘッダと帯の高さは、画面の幅で折り返して変わるので、測って CSS 変数に入れる(項目の一覧・プレビューの固定の位置にも使う)。

import { type ReactNode, useRef } from "react";
import { useHeightVar } from "./heightVar";

export function Toolbar({ name, id, children }: { name: string; id: string; children: ReactNode }) {
  const bar = useRef<HTMLDivElement>(null);
  useHeightVar(bar, "--dz-toolbar-h");
  return (
    <div className="dz-toolbar" ref={bar}>
      <p className="dz-title">{name}({id})</p>
      <div className="dz-actions">{children}</div>
    </div>
  );
}
