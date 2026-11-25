// 確認の画面(モーダル)。ブラウザ標準の <dialog> の showModal で出し、出ている間は、後ろの画面を操作できなくする
// (クリックも、キーボードでの移動もできない)。Esc は「やめる」と同じ。

import { type ReactNode, useEffect, useRef } from "react";

export function Modal({ labelledBy, onCancel, children }: { labelledBy: string; onCancel: () => void; children: ReactNode }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = dialog.current;
    if (element && !element.open) element.showModal();
    return () => element?.close();
  }, []);
  return (
    <dialog ref={dialog} className="dz-dialog dz-modal" aria-labelledby={labelledBy}
      onCancel={(e) => {
        e.preventDefault(); // 閉じるのは、呼び出し側の状態で決める
        onCancel();
      }}>
      {children}
    </dialog>
  );
}
