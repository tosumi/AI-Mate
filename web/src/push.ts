// この端末(ブラウザ)で通知を受け取る・やめる(本体仕様書 §15.1 の M8)。
// 購読(送り先と鍵)はサーバーに送るが、ブラウザの保存領域には置かない(ブラウザ自身が持つ)。

import type { Api } from "./api";

export type PushState = "unsupported" | "disabled" | "denied" | "off" | "on";

const SW_URL = "/sw.js";

export function pushSupported(): boolean {
  return typeof window !== "undefined" && "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
}

/** base64url の文字列を、バイト列にする(applicationServerKey 用)。 */
export function base64UrlToBytes(text: string): Uint8Array<ArrayBuffer> {
  const padded = (text + "=".repeat((4 - (text.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(padded);
  const bytes = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  return bytes;
}

async function registration(): Promise<ServiceWorkerRegistration | undefined> {
  return navigator.serviceWorker.getRegistration(SW_URL);
}

/** いまの状態。サーバーが通知を使えないときは "disabled"。 */
export async function pushState(api: Api): Promise<PushState> {
  if (!pushSupported()) return "unsupported";
  const config = await api.pushConfig();
  if (!config.enabled) return "disabled";
  if (Notification.permission === "denied") return "denied";
  const existing = await (await registration())?.pushManager.getSubscription();
  return existing ? "on" : "off";
}

/** 通知を受け取る(ブラウザの許可を求め、購読を作って、サーバーに登録する)。 */
export async function enablePush(api: Api): Promise<PushState> {
  const config = await api.pushConfig();
  if (!config.enabled || !config.public_key) return "disabled";
  const permission = await Notification.requestPermission();
  if (permission !== "granted") return permission === "denied" ? "denied" : "off";
  const reg = (await registration()) ?? (await navigator.serviceWorker.register(SW_URL));
  await navigator.serviceWorker.ready;
  const subscription =
    (await reg.pushManager.getSubscription()) ??
    (await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: base64UrlToBytes(config.public_key) }));
  await api.subscribePush(subscription.toJSON());
  return "on";
}

/** 通知を受け取るのをやめる(サーバーの登録を消してから、ブラウザの購読を消す)。 */
export async function disablePush(api: Api): Promise<PushState> {
  const subscription = await (await registration())?.pushManager.getSubscription();
  if (subscription) {
    await api.unsubscribePush(subscription.endpoint);
    await subscription.unsubscribe();
  }
  return "off";
}
