// 通知(Web Push)を受け取る Service Worker(本体仕様書 §15.1 の M8)。
// 通知の本文には、会話の内容は入っていない(キャラクタ名と、メッセージがあることだけ)。
// ページのキャッシュ・オフラインの動作はしない(通知の表示と、押したときに画面を開くことだけ)。

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = {};
  }
  const title = typeof data.title === "string" ? data.title : "FureAI ペルソナ";
  const body = typeof data.body === "string" ? data.body : "メッセージがあります";
  event.waitUntil(
    Promise.all([
      self.registration.showNotification(title, {
        body,
        tag: "mycompanion-message", // 同じ種類の通知は、まとめて1つにする
        data: { url: "/chat" },
      }),
      // 開いている画面に知らせて、メッセージを取りに行かせる(本文は渡さない。画面がサーバーから読む)
      tell(),
    ]),
  );
});

function tell() {
  return self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
    for (const w of windows) {
      if (new URL(w.url).origin === self.location.origin) w.postMessage({ type: "mycompanion-message" });
    }
    return windows;
  });
}

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  // 開く先は、自分のサイトの中だけ
  const url = new URL("/chat", self.location.origin).href;
  // 開いている画面があれば、前に出して、メッセージを取りに行かせる(読み直さないので、サインインや入力中の文字が残る)
  event.waitUntil(
    tell().then((windows) => {
      const open = windows.find((w) => {
        const u = new URL(w.url);
        return u.origin === self.location.origin && (u.pathname === "/chat" || u.pathname.startsWith("/chat/"));
      });
      return open ? open.focus() : self.clients.openWindow(url);
    }),
  );
});
