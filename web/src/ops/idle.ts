// 操作がない時間を測る(運営者の画面。30分操作がなければ、画面を消してサインアウトする。2026-11-24 のユーザーの決定)。
// タブが裏にあるあいだはタイマーが遅れるので、表に戻ったときにも、最後の操作からの時間で確かめる。

import { useEffect, useRef } from "react";

export const IDLE_LIMIT_MS = 30 * 60 * 1000;
const CHECK_MS = 15 * 1000;
const ACTIVITY_EVENTS = ["pointerdown", "keydown", "wheel", "touchstart", "scroll"] as const;

export function isIdle(lastActivity: number, now: number, limitMs = IDLE_LIMIT_MS): boolean {
  return now - lastActivity >= limitMs;
}

/** `active` のあいだ、操作がない時間が `limitMs` を超えたら、`onIdle` を1回呼ぶ。 */
export function useIdleTimeout(active: boolean, onIdle: () => void, limitMs = IDLE_LIMIT_MS): void {
  const onIdleRef = useRef(onIdle);
  onIdleRef.current = onIdle;

  useEffect(() => {
    if (!active) return;
    let last = Date.now();
    let fired = false;
    const touch = () => {
      last = Date.now();
    };
    const check = () => {
      if (!fired && isIdle(last, Date.now(), limitMs)) {
        fired = true;
        onIdleRef.current();
      }
    };
    const onVisible = () => {
      if (document.visibilityState === "visible") check();
    };
    for (const name of ACTIVITY_EVENTS) window.addEventListener(name, touch, { passive: true, capture: true });
    document.addEventListener("visibilitychange", onVisible);
    const timer = window.setInterval(check, CHECK_MS);
    return () => {
      for (const name of ACTIVITY_EVENTS) window.removeEventListener(name, touch, { capture: true });
      document.removeEventListener("visibilitychange", onVisible);
      window.clearInterval(timer);
    };
  }, [active, limitMs]);
}
