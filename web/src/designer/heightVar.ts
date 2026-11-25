// 要素の高さを CSS 変数に入れる(ヘッダ・上部の帯は、画面の幅で折り返して高さが変わる)。

import { type RefObject, useEffect } from "react";

/** 要素の高さを、いちばん外側(.dz)の CSS 変数に入れ続ける。外れたら 0 に戻す。 */
export function useHeightVar(ref: RefObject<HTMLElement | null>, name: string) {
  useEffect(() => {
    const element = ref.current;
    const root = element?.closest<HTMLElement>(".dz");
    if (!element || !root) return;
    const update = () => root.style.setProperty(name, `${element.getBoundingClientRect().height}px`);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => {
      observer.disconnect();
      root.style.setProperty(name, "0px");
    };
  }, [ref, name]);
}
