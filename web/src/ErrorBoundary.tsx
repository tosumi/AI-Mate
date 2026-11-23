// 画面の部品のエラーの受け皿。1つの部品のエラーで、画面全体が真っ白にならないようにする。
// エラーの中身(利用者の入力を含みうる)は、画面にもログにも出さない。

import { Component, type ReactNode } from "react";

type Props = { children: ReactNode };
type State = { failed: boolean };

export class ErrorBoundary extends Component<Props, State> {
  state: State = { failed: false };

  static getDerivedStateFromError(): State {
    return { failed: true };
  }

  render() {
    if (this.state.failed) {
      return (
        <div role="alert" className="error">
          <p>画面の表示で問題が起きました。</p>
          <button onClick={() => window.location.reload()}>読み込み直す</button>
        </div>
      );
    }
    return this.props.children;
  }
}
