// 新しい版の表示(本体仕様書 §15.1 の M10 の手順1)。
// 配信のあと、開いたままの画面は古い版のまま動く。画面に戻ったとき・一定の時間ごとに /version.json を読み、
// 自分の版と違えば「新しい版があります」を出す(自動では読み込み直さない。入力中の文字を消さないため)。

import { useEffect, useState } from "react";

declare const __APP_VERSION__: string;

export const APP_VERSION: string = typeof __APP_VERSION__ === "string" ? __APP_VERSION__ : "test";
const CHECK_MS = 10 * 60 * 1000;

/** 配信されている版が、この画面の版と違うか。読めないとき(開発用のサーバーなど)は false。 */
export async function isOutdated(fetcher: typeof fetch = (...args) => fetch(...args)): Promise<boolean> {
  try {
    const response = await fetcher("/version.json", { cache: "no-store" });
    if (!response.ok) return false;
    const body = (await response.json()) as { version?: unknown };
    return typeof body.version === "string" && body.version !== "" && body.version !== APP_VERSION;
  } catch {
    return false;
  }
}

export function useNewVersion(): boolean {
  const [outdated, setOutdated] = useState(false);
  useEffect(() => {
    if (outdated) return;
    let alive = true;
    const check = async () => {
      if (document.visibilityState !== "visible") return;
      if ((await isOutdated()) && alive) setOutdated(true);
    };
    const onVisible = () => void check();
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("focus", onVisible);
    const timer = window.setInterval(onVisible, CHECK_MS);
    return () => {
      alive = false;
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("focus", onVisible);
      window.clearInterval(timer);
    };
  }, [outdated]);
  return outdated;
}
