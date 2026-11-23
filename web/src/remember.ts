// 画面の小さな状態(見ていたページ、選んでいたキャラクタ)を、タブの一時保存領域に残す(再表示のときに戻すため)。
// 会話の内容は残さない(会話の続きは、サーバーから読み直す)。保存できないブラウザでも、画面は動く。

const PREFIX = "mycompanion.";

export function remember(key: string, value: string): void {
  try {
    sessionStorage.setItem(PREFIX + key, value);
  } catch {
    // 保存できない(プライベートモードなど)ときは、覚えないだけ
  }
}

export function recall(key: string): string | null {
  try {
    return sessionStorage.getItem(PREFIX + key);
  } catch {
    return null;
  }
}
