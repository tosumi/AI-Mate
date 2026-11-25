// 新しい版があるときの帯(一般ユーザー・運営者の画面で共通。§15.1 の M10 の手順1)。

import { useNewVersion } from "./version";

/** 新しい版があるときの帯。 */
export function NewVersionBanner({ className = "update-banner" }: { className?: string }) {
  const outdated = useNewVersion();
  if (!outdated) return null;
  return (
    <div className={className} role="status">
      <span>新しい版があります。</span>
      <button onClick={() => window.location.reload()}>読み込み直す</button>
    </div>
  );
}
