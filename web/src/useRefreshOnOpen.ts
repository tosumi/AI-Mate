// ページを開くたびに、前の内容を出したまま、裏で新しくする(最初の読み込みは、各画面が自分で行う)。

import { useEffect, useRef } from "react";

export function useRefreshOnOpen(active: boolean, refresh: () => void): void {
  const mounted = useRef(false);
  const latest = useRef(refresh);
  latest.current = refresh;
  useEffect(() => {
    if (!mounted.current) {
      mounted.current = true;
      return;
    }
    if (active) latest.current();
  }, [active]);
}
