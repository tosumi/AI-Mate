"""CLI(API を呼ぶ薄いクライアント。本体仕様書 §10.3、§11.5.8)。

M0 の範囲: `login` / `logout` / `whoami` / `health`。会話(`chat`)は M1 以降で追加する。

- サインインはデバイスコード方式(§13.4.3)。
- トークンは OS の保護機構(Windows は DPAPI、macOS はキーチェーン、Linux は libsecret)で
  暗号化して保存し、平文のファイルには置かない。
- エラーは必ず stderr に出す(無言の表示と混同しないため。§10.2)。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer

from mycompanion.cli import persona as persona_cli
from mycompanion.config import CliConfig, ConfigError

app = typer.Typer(help="MyCompanion の CLI", no_args_is_help=True, add_completion=False)
app.add_typer(persona_cli.app, name="persona")

ApiUrlOption = Annotated[
    str | None,
    typer.Option("--api-url", help="API の URL(省略時は環境変数 MYCOMPANION_API_URL)"),
]


def _fail(message: str, code: int = 1) -> typer.Exit:
    print(f"エラー: {message}", file=sys.stderr)
    return typer.Exit(code)


def _config(api_url: str | None) -> CliConfig:
    try:
        return CliConfig.from_env(api_url=api_url)
    except ConfigError as exc:
        raise _fail(str(exc), 2) from None


# ---------------------------------------------------------------------------
# トークンの取得と保存
# ---------------------------------------------------------------------------


def _token_cache() -> Any:
    """OS の保護機構で暗号化したトークンキャッシュ。平文への退避はしない。"""
    from msal_extensions import PersistedTokenCache, build_encrypted_persistence

    folder = Path(typer.get_app_dir("mycompanion"))
    folder.mkdir(parents=True, exist_ok=True)
    persistence = build_encrypted_persistence(str(folder / "token_cache.bin"))
    return PersistedTokenCache(persistence)


def _msal_app(config: CliConfig) -> Any:
    import msal

    missing = [
        name
        for name, value in [
            ("MYCOMPANION_CLI_CLIENT_ID", config.client_id),
            ("ENTRA_EXTERNAL_AUTHORITY", config.authority),
            ("ENTRA_EXTERNAL_TENANT_ID", config.tenant_id),
            ("ENTRA_API_SCOPE", config.api_scope),
        ]
        if not value
    ]
    if missing:
        raise _fail(f"サインインに必要な環境変数が設定されていません: {', '.join(missing)}", 2)
    assert config.authority and config.tenant_id
    return msal.PublicClientApplication(
        config.client_id,
        authority=f"{config.authority.rstrip('/')}/{config.tenant_id}",
        token_cache=_token_cache(),
    )


def _access_token(config: CliConfig) -> str:
    """保存済みのトークンを(必要なら更新して)返す。サインインしていなければ終了する。"""
    client = _msal_app(config)
    accounts = client.get_accounts()
    if accounts:
        result = client.acquire_token_silent([config.api_scope], account=accounts[0])
        if result and "access_token" in result:
            return result["access_token"]
    raise _fail("サインインしていません。`companion login` を実行してください。")


def _get(config: CliConfig, path: str, token: str | None = None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        return httpx.get(f"{config.api_url}{path}", headers=headers, timeout=15)
    except httpx.HTTPError as exc:
        raise _fail(f"API に接続できません({type(exc).__name__})") from None


# ---------------------------------------------------------------------------
# コマンド
# ---------------------------------------------------------------------------


@app.command()
def login(api_url: ApiUrlOption = None) -> None:
    """サインインする(デバイスコード方式)。"""
    config = _config(api_url)
    client = _msal_app(config)
    flow = client.initiate_device_flow(scopes=[config.api_scope])
    if "user_code" not in flow:
        raise _fail("デバイスコードを取得できませんでした。アプリ登録の設定を確認してください。")
    # 表示するのは、確認用の URL とコードだけ
    print(flow["message"])
    result = client.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise _fail(f"サインインに失敗しました({result.get('error', 'unknown')})")
    print("サインインしました。")


@app.command()
def logout(api_url: ApiUrlOption = None) -> None:
    """サインアウトする(保存済みのトークンを削除する)。"""
    config = _config(api_url)
    client = _msal_app(config)
    accounts = client.get_accounts()
    for account in accounts:
        client.remove_account(account)
    print("サインアウトしました。" if accounts else "サインインしていません。")


@app.command()
def whoami(api_url: ApiUrlOption = None) -> None:
    """現在のアカウント(内部のユーザーID)を表示する。"""
    config = _config(api_url)
    response = _get(config, "/v1/me", _access_token(config))
    if response.status_code == 401:
        raise _fail("認証に失敗しました。`companion login` をやり直してください。")
    if response.status_code != 200:
        raise _fail(f"API がエラーを返しました(HTTP {response.status_code})")
    print(f"user_id: {response.json()['user_id']}")


@app.command()
def health(
    api_url: ApiUrlOption = None,
    ready: Annotated[bool, typer.Option("--ready", help="依存先(DB など)への疎通も確認する")] = False,
) -> None:
    """API のヘルスチェックを呼ぶ。"""
    config = _config(api_url)
    response = _get(config, "/readyz" if ready else "/healthz")
    body = response.json()
    if response.status_code != 200:
        raise _fail(f"API は準備できていません(HTTP {response.status_code}): {body.get('detail')}")
    print(f"status: {body['status']}")
    for name, ok in (body.get("checks") or {}).items():
        print(f"  {name}: {'ok' if ok else 'ng'}")


def _post(config: CliConfig, path: str, token: str, body: dict[str, Any], **params: Any) -> httpx.Response:
    try:
        return httpx.post(
            f"{config.api_url}{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
            params=params or None,
            timeout=60,
        )
    except httpx.HTTPError as exc:
        raise _fail(f"API に接続できません({type(exc).__name__})") from None


# デバッグ表示のマーカー(§10.4)
MARKERS = {"filtered": "🎭", "draft": "📝", "silence": "🤐", "backchannel": "💬"}


def render_actions(output: dict[str, Any], debug: bool, silence_render: str = "dots") -> list[str]:
    """応答を、CLI の表示に変換する(§10.2、§10.4)。表示形式はアダプタが決める。"""
    lines: list[str] = []
    for action in output.get("actions", []):
        kind = action.get("type")
        # マーカーは、応答の種類ごとに付ける。`both` では、下書きとフィルタ後の両方に付く(§10.4)
        symbol = (
            MARKERS.get(action.get("variant", "filtered")) if kind == "message" else MARKERS.get(kind or "")
        )
        marker = f" [{symbol}]" if debug and symbol else ""
        if kind in ("message", "backchannel"):
            lines.append(f"{action['text']}{marker}")
        elif kind == "silence":
            # 無言は「・・・」で表示する(silence_render=none なら何も出さない)
            if silence_render != "none" and action.get("render") != "none":
                lines.append(f"・・・{marker}")
        elif kind == "system_notice":
            # システム通知は、キャラクタの発話と区別して表示する(§8.8.7)
            lines.append(f"【お知らせ】{action['text']}")
    if output.get("edit_summary"):
        lines.append(f"  (フィルタの変更: {output['edit_summary']})")
    return lines


def render_debug(info: dict[str, Any]) -> list[str]:
    """感情推定の結果の要約(デバッグ表示)。"""
    estimate = info["estimate"]
    top = sorted(estimate["categories"].items(), key=lambda kv: kv[1], reverse=True)[:3]
    categories = " ".join(f"{name}={value:.2f}" for name, value in top if value > 0) or "-"
    vad = estimate["vad"]
    topics = "、".join(t["label"] for t in estimate["topics"]) or "-"
    request = estimate["persona_request"]
    risk = {k: v for k, v in estimate["risk"].items() if v >= 0.3}
    vad_text = f"V={vad['valence']:+.2f} A={vad['arousal']:+.2f} D={vad['dominance']:+.2f}"
    lines = [
        f"  感情: {categories} / {vad_text} / 確信度={estimate['confidence']:.2f} gap={estimate['gap']:.2f}",
        f"  欲求: {','.join(estimate['needs']) or '-'} / 向き: {estimate['target']} / 話題: {topics}",
    ]
    if estimate.get("cause"):
        lines.append(f"  原因の仮説: {estimate['cause']}")
    if request["kind"] != "none":
        lines.append(f"  人格への要求: {request['kind']}(確信度 {request['confidence']:.2f})")
    if risk:
        lines.append("  リスク: " + " ".join(f"{k}={v:.2f}" for k, v in risk.items()))
    if info.get("degraded"):
        lines.append("  ※ 推定が縮退動作になりました")
    state = info.get("state") or {}
    if state.get("restricted"):
        return ["  ※ 制限中のため、応答を生成していません"]
    if state:
        relation = state["relationship"]
        effective = state["effective"]
        companion = state["companion"]
        feelings = sorted(companion["categories"].items(), key=lambda kv: kv[1], reverse=True)[:2]
        feeling_text = " ".join(f"{name}={value:.2f}" for name, value in feelings if value >= 0.05) or "-"
        lines.append(
            f"  関係値: {relation['value']:+.1f}({relation['band']}、今回 {relation['delta']:+.2f})"
            f" / 口数 {effective['talkativeness']:.2f}({effective['length']})"
            f" 表出 {effective['expressiveness']:.2f} / 同期 {state['sync_weight']:.2f}"
        )
        rules = "、".join(rule["topic"] for rule in companion["rules"])
        lines.append(f"  キャラクタの感情: {feeling_text}" + (f" / 反応した話題: {rules}" if rules else ""))
        # 好き嫌いシート由来の反応。発火したものと、止めたもの(理由つき)
        liked = [h for h in companion.get("sheet") or [] if "delta" in h]
        if liked:
            changes = " ".join(f"{k} {v:+.2f}" for k, v in (companion.get("sheet_effects") or {}).items())
            names = "、".join(h["catalog_id"] + ("(話題を変えたい)" if h.get("avoid_flag") else "") for h in liked)
            lines.append(f"  好き嫌いへの反応: {changes or '(上限で 0)'} / {names}")
        held = [h for h in companion.get("sheet") or [] if "suppressed_by" in h]
        if held:
            lines.append("  止めた好き嫌いの反応: " + "、".join(f"{h['catalog_id']}({h['suppressed_by']})" for h in held))
        # イベントルール(出来事への反応)。発火したものと、止めたもの(理由つき)
        fired = [e for e in companion.get("events") or [] if "delta" in e]
        if fired:
            changes = " ".join(f"{k} {v:+.2f}" for k, v in (companion.get("event_effects") or {}).items())
            names = "、".join(f"{e['rule_id']}({e['trigger']}・{e['n'] + 1}回目)" for e in fired)
            lines.append(f"  感情の変化: {changes or '(上限で 0)'} / きっかけ: {names}")
        stopped = [e for e in companion.get("events") or [] if "suppressed_by" in e]
        if stopped:
            lines.append("  止めた反応: " + "、".join(f"{e['rule_id']}({e['suppressed_by']})" for e in stopped))
        plan, gate = state.get("plan"), state.get("gate")
        if plan:
            expression = plan.get("companion_expression")
            shown = f"{expression['emotion']}({expression['level']:.2f})" if expression else "なし"
            avoid = "、".join(plan.get("avoid") or []) or "-"
            lines.append(
                f"  方針: {plan['strategy']} / トーン {plan['tone']} / 長さ {plan['length']}"
                f" / 確認 {'あり' if plan.get('ask_clarify') else 'なし'} / 表出 {shown} / 避ける: {avoid}"
            )
        if gate:
            hits = ",".join(gate.get("rule_hits") or []) or "-"
            reason = f" 理由 {gate['reason']}" if gate.get("reason") else ""
            lines.append(
                f"  応答モード: {gate['mode']}(無言スコア {gate['score']:+.2f}、無言を禁じた規則: {hits}{reason})"
            )
        screening = state.get("screening") or []
        memory = state.get("memory") or {}
        if memory.get("retrieved"):
            # 参照した件数と、この応答で実際に使った記憶の内容を表示する(毎回同じ一覧は出さない)
            used = set(memory.get("used") or [])
            names = {"core": "大切なこと", "short": "短期", "medium": "中期", "working_set": "直前の話題", "topic": "話題"}
            counts: dict[str, int] = {}
            for item in memory["retrieved"]:
                label = names.get(item["slot"], "長期")
                counts[label] = counts.get(label, 0) + 1
            total = "、".join(f"{label} {count}" for label, count in counts.items())
            texts = [f"「{m.get('text', m['handle'])}」" for m in memory["retrieved"] if m["handle"] in used]
            lines.append(f"  記憶: {len(memory['retrieved'])}件を参照({total})/ 使ったもの: {' '.join(texts) or 'なし'}")
            if memory.get("topic_offered"):
                lines.append("  話題: キャラクタから話題を振りました")
        if screening:
            lines.append("  検出: " + "、".join(f"{s['category']}→{s['action']}" for s in screening))
        flags = [f for f in state.get("safety_flags") or [] if f != "emotion_degraded"]
        if flags:
            lines.append("  フラグ: " + "、".join(flags))
    return lines


def render_relationship(view: dict[str, Any]) -> list[str]:
    """関係性の概要(§10.3 の /relation show)。数値は、デバッグ表示のときだけ含まれる。"""
    lines = [f"関係: {view['band']}", f"  {view['description']}"]
    if "value" in view:
        lines.append(
            f"  関係値 {view['value']:+.1f} / 口数 {view['talkativeness_eff']:.2f}"
            f" / 表出 {view['expressiveness_eff']:.2f}"
        )
    return lines


TIER_NAMES = {"short": "短期", "medium": "中期", "long": "長期"}


def render_memories(items: list[dict[str, Any]], debug: bool = False) -> list[str]:
    """記憶の一覧(§10.3 の /memory list)。ID は、先頭の8文字を表示する。"""
    if not items:
        return ["記憶は、まだありません。"]
    lines = []
    for item in items:
        marks = "".join(mark for flag, mark in (("core", "⭐"), ("pinned", "📌"), ("muted", "🔇")) if item.get(flag))
        extra = ""
        if debug:
            extra = f"  [活性度 {item.get('activation', 0):.2f} / 嗜好度 {item.get('preference', 0):+.2f}]"
            if item.get("quarantined"):
                extra += " [隔離]"
        label = f"{item['period']}の概要" if item["kind"] == "monthly_summary" else item["kind"]
        lines.append(f"  {item['id'][:8]}  {TIER_NAMES[item['tier']]}  {label}  {item['summary']} {marks}{extra}".rstrip())
    return lines


def render_memory(item: dict[str, Any]) -> list[str]:
    lines = [
        f"ID: {item['id']}",
        f"層: {TIER_NAMES[item['tier']]} / 種類: {item['kind']} / 重要度: {item['importance']:.2f}",
        f"内容: {item['summary']}",
        f"話題: {'、'.join(str(t) for t in item.get('topics') or []) or '-'}",
    ]
    if "activation" in item:
        lines.append(
            f"活性度: {item['activation']:.2f} / 嗜好度: {item['preference']:+.2f}"
            f" / 想起日数: {item['recall_days']}(うちユーザー起点 {item['user_recall_days']})"
        )
        for recall in item.get("recalls") or []:
            lines.append(f"  想起 {recall['day']}  起点 {recall['trigger']}  {recall['source']}  {recall['status']}")
    return lines


def render_topics(data: dict[str, Any]) -> list[str]:
    """トピックの一覧(`/memory topics`)。アーカイブは、概要の一覧。"""
    if "archived" in data:
        if not data["archived"]:
            return ["削除(アーカイブ)したトピックは、ありません。"]
        lines = ["削除したトピック(概要だけを残しています。会話では使いません):"]
        for item in data["archived"]:
            period = f"{item['first_occurred']}〜{item['last_occurred']}" if item.get("first_occurred") else "-"
            summary = item["summary"] or "(概要なし)"
            lines.append(f"  {item['id']}  {item['title']}({period}) {summary}")
        return lines
    if not data.get("topics"):
        return ["トピック(話題ごとの経過)は、まだありません。"]
    lines = ["話題ごとの経過(確認: /memory topic show <ID>、削除: /memory topic forget <ID>):"]
    for item in data["topics"]:
        kind = "人・もの" if item["kind"] == "entity" else "出来事"
        marks = "".join([" [候補]" if item["status"] == "draft" else "", " [持ち出さない]" if item["muted"] else ""])
        lines.append(f"  {item['id']}  [{kind}] {item['title']}({item['entries']}件){marks} {item['description']}")
    return lines


def render_topic(item: dict[str, Any]) -> list[str]:
    if item.get("archived"):
        return [f"ID: {item['id']}(削除済み。概要だけを残しています)", f"名前: {item['title']}",
                f"概要: {item['summary'] or '(概要なし)'}"]  # fmt: skip
    lines = [f"ID: {item['id']}", f"名前: {item['title']}", f"説明: {item['description'] or '-'}"]
    if item.get("aliases"):
        lines.append(f"別名: {'、'.join(item['aliases'])}")
    for relation in item.get("relations") or []:
        lines.append(f"  関係 {relation['id']}  {relation['label']}: {relation['title']}({relation['to']})")
    for entry in item.get("timeline") or []:
        lines.append(f"  {entry['id']}  {entry['when']}(話した日 {entry['told_on']}) {entry['kind']}  {entry['text']}")
    return lines


def render_emotions(data: dict[str, Any]) -> list[str]:
    if not data.get("days"):
        return ["感情の推定の記録は、ありません。"]
    lines = ["日ごとの要約(推定です。断定ではありません):"]
    for day in data["days"]:
        vad = day["vad"]
        main = "、".join(day["main"]) or "-"
        lines.append(
            f"  {day['day']}  {day['count']}件  主な感情: {main}"
            f"  快・不快 {vad['valence']:+.2f} / 高ぶり {vad['arousal']:+.2f}"
        )
    lines.append("最近の記録(削除は /emotion forget <ID>、または /emotion forget --day YYYY-MM-DD):")
    for event in data.get("recent", [])[:5]:
        lines.append(f"  {event['id']}  {event['ts'][:16]}")
    return lines


def render_runs(runs: list[dict[str, Any]]) -> list[str]:
    if not runs:
        return ["整理する記憶は、ありません。"]
    names = {"success": "完了", "skipped": "対象なし", "failed": "失敗"}
    lines = []
    for run in runs:
        counts = " ".join(f"{k}={v}" for k, v in (run.get("counts") or {}).items() if v)
        error = f"({run['error']})" if run.get("error") else ""
        kind = "月次" if run["type"] == "monthly" else "日次"
        lines.append(f"  {kind} {run['target']}: {names.get(run['status'], run['status'])}{error} {counts}".rstrip())
    return lines


def _request(config: CliConfig, method: str, path: str, **kwargs: Any) -> Any | None:
    """API を呼び、JSON を返す。エラーは stderr に出して None を返す。"""
    headers = {"Authorization": f"Bearer {_access_token(config)}"}
    try:
        response = httpx.request(method, f"{config.api_url}{path}", headers=headers, timeout=120, **kwargs)
    except httpx.HTTPError as exc:
        print(f"エラー: API に接続できません({type(exc).__name__})", file=sys.stderr)
        return None
    if response.status_code != 200:
        try:
            message = response.json().get("message")
        except ValueError:
            message = None
        print(f"エラー: {message or f'HTTP {response.status_code}'}", file=sys.stderr)
        return None
    return response.json()


def _option(parts: list[str], name: str) -> str | None:
    """`--tier short` のような指定を取り出す。"""
    if name in parts and parts.index(name) + 1 < len(parts):
        return parts[parts.index(name) + 1]
    return None


def _memory_command(config: CliConfig, character: str, text: str, debug: bool) -> None:
    """対話中の /memory と /emotion(§10.3)。"""
    parts = text.split()
    params: dict[str, Any] = {"character_id": character, "debug": str(debug).lower()}
    usage = (
        "使い方: /memory list|show|forget|mute|unmute|edit|promote、/memory topics [--archived|--all]、"
        "/memory topic show|forget|mute|unmute <ID>、/memory topic unlink <ID> <関係のID>、/emotion list|forget"
    )
    if len(parts) < 2:
        print(usage, file=sys.stderr)
        return
    command, action = parts[0], parts[1]
    if command == "/emotion":
        if action == "list":
            days = _option(parts, "--days")
            data = _request(config, "GET", "/v1/emotion", params={"character_id": character, "days": days or 7})
            for line in render_emotions(data) if data is not None else []:
                print(line)
        elif action == "forget" and len(parts) >= 3:
            day = _option(parts, "--day")
            query = {"character_id": character, **({"day": day} if day else {"event_id": parts[2]})}
            data = _request(config, "DELETE", "/v1/emotion", params=query)
            if data is not None:
                print(f"感情の記録を {data['deleted']}件、削除しました。")
        else:
            print(usage, file=sys.stderr)
        return

    if action == "topics":
        query = {"character_id": character, "archived": str("--archived" in parts).lower(),
                 "candidates": str("--all" in parts).lower()}  # fmt: skip
        data = _request(config, "GET", "/v1/topics", params=query)
        for line in render_topics(data) if data is not None else []:
            print(line)
        return
    if action == "topic":
        if len(parts) < 4:
            print(usage, file=sys.stderr)
            return
        verb, topic_id = parts[2], parts[3]
        scope = {"character_id": character}
        if verb == "show":
            data = _request(config, "GET", f"/v1/topics/{topic_id}", params=scope)
            for line in render_topic(data) if data is not None else []:
                print(line)
        elif verb == "unlink" and len(parts) >= 5:
            if _request(config, "DELETE", f"/v1/topics/{topic_id}/relations/{parts[4]}", params=scope) is not None:
                print("関係を取り消しました。")
        elif verb in ("forget", "purge"):
            data = _request(config, "DELETE", f"/v1/topics/{topic_id}", params=scope)
            if data is not None:
                if data.get("purged"):
                    print("アーカイブを、完全に削除しました。")
                else:
                    print(
                        f"トピック「{data['title']}」を削除しました。概要だけを残し、会話では使いません(ID: {data['id']})。"
                        f"完全に消すには /memory topic purge {data['id']}"
                    )
        elif verb in ("mute", "unmute"):
            if _request(config, "POST", f"/v1/topics/{topic_id}/{verb}", params=scope) is not None:
                done = {"mute": "この話題を、キャラクタが自分から持ち出さないようにしました。",
                        "unmute": "この話題を、キャラクタが持ち出せるように戻しました。"}  # fmt: skip
                print(done[verb])
        else:
            print(usage, file=sys.stderr)
        return
    if action == "list":
        for name in ("tier", "kind"):
            if value := _option(parts, f"--{name}"):
                params[name] = value
        data = _request(config, "GET", "/v1/memory", params=params)
        for line in render_memories(data, debug) if data is not None else []:
            print(line)
        return
    if len(parts) < 3:
        print(usage, file=sys.stderr)
        return
    memory_id = parts[2]
    if action == "show":
        data = _request(config, "GET", f"/v1/memory/{memory_id}", params=params)
        for line in render_memory(data) if data is not None else []:
            print(line)
    elif action == "forget":
        if _request(config, "DELETE", f"/v1/memory/{memory_id}", params={"character_id": character}) is not None:
            print("記憶を削除しました(この記憶に由来する要約などからも、取り除きます)。")
    elif action in ("mute", "unmute", "promote"):
        data = _request(config, "POST", f"/v1/memory/{memory_id}/{action}", params={"character_id": character})
        if data is not None:
            done = {"mute": "この記憶を、キャラクタが自分から持ち出さないようにしました。",
                    "unmute": "この記憶を、キャラクタが持ち出せるように戻しました。",
                    "promote": "この記憶を、長期に昇格しました。"}  # fmt: skip
            print(done[action])
    elif action == "edit":
        summary = " ".join(parts[3:]) or input("訂正後の内容> ").strip()
        body = {"character_id": character, "summary": summary}
        data = _request(config, "PUT", f"/v1/memory/{memory_id}", json=body)
        if data is not None:
            print(f"訂正しました: {data['summary']}")
    else:
        print(usage, file=sys.stderr)


account_app = typer.Typer(help="アカウント(同意、年齢、データの書き出し、削除)", no_args_is_help=True)
app.add_typer(account_app, name="account")


def _show_screen(screen: dict[str, Any]) -> None:
    print(f"\n━━ {screen['title']} ━━")
    if screen["text"]:
        print(screen["text"])


def _ask_items(screen: dict[str, Any], yes: bool, optional: set[str]) -> list[str]:
    """画面のチェックボックスを、1つずつ尋ねる。同意した種類を返す。"""
    accepted = []
    for item in screen["items"]:
        if item["granted"]:
            print(f"  [同意済み] {item['label']}")
        elif yes:
            # --yes は、必須の項目だけに同意する。任意の項目は、オプションで明示したものだけ
            if item["required"] or item["type"] in optional:
                accepted.append(item["type"])
        elif input(f"  {'' if item['required'] else '(任意)'}{item['label']} [y/N] > ").strip().lower() in ("y", "yes"):
            accepted.append(item["type"])
    return accepted


@account_app.command("consent")
def account_consent(
    yes: Annotated[bool, typer.Option("--yes", help="確認の質問をせずに、必須の項目すべてに同意する")] = False,
    revoke: Annotated[str | None, typer.Option("--revoke", help="指定した種類の同意を撤回する")] = None,
    adult: Annotated[
        str | None, typer.Option("--adult", help="「18歳以上ですか」への答え: yes / no / skip(省略時は、質問する)")
    ] = None,
    sensitive_memory: Annotated[
        bool, typer.Option("--sensitive-memory", help="(18歳以上・任意)センシティブな内容を記憶に残すことに同意する")
    ] = False,
    romantic: Annotated[
        bool, typer.Option("--romantic", help="(18歳以上・任意)キャラクタの恋愛的な表現を有効にする")
    ] = False,
    api_url: ApiUrlOption = None,
) -> None:
    """利用に必要な同意と、年齢の質問に答える。会話を始める前に、1回だけ実行する。"""
    config = _config(api_url)
    if revoke:
        data = _request(config, "DELETE", f"/v1/account/consents/{revoke}")
        if data is None:
            raise typer.Exit(1)
        print(f"同意を撤回しました: {revoke}")
        if data.get("sensitive_memories_deleted"):
            print(f"保存していたセンシティブな記憶を {data['sensitive_memories_deleted']}件、削除しました。")
        return
    if adult not in (None, "yes", "no", "skip"):
        raise _fail("--adult は yes / no / skip のいずれかで指定してください")
    optional = {name for name, on in (("sensitive_memory", sensitive_memory), ("romantic_expression", romantic)) if on}

    def fetch() -> dict[str, Any]:
        data = _request(config, "GET", "/v1/account/consents")
        if data is None:
            raise typer.Exit(1)
        return data

    def grant(kinds: list[str]) -> None:
        if kinds and _request(config, "POST", "/v1/account/consents", json={"types": kinds, "channel": "cli"}) is None:
            raise typer.Exit(1)

    state = fetch()
    print(f"同意書(版 {state['version']})。※ 文面はドラフトで、【 】は未確定の箇所です。")
    # 画面1〜3: 利用規約、感情の推定と記憶、AI サービスへの送信と処理の場所
    for screen in state["screens"]:
        if screen["id"] in ("1", "2", "3"):
            _show_screen(screen)
            grant(_ask_items(screen, yes, optional))

    # 画面4: 年齢の質問。「いいえ」「答えない」は、18歳未満の扱い
    age_screen = next((sc for sc in state["screens"] if sc["id"] == "4"), None)
    if adult is None and not yes:
        if age_screen:
            _show_screen(age_screen)
        current = "18歳未満の扱い" if state["is_under_18"] else "18歳以上"
        adult = input(f"  [yes / no / skip](現在: {current}。Enter でそのまま) > ").strip().lower() or None
        if adult not in (None, "yes", "no", "skip"):
            raise _fail("yes / no / skip のいずれかで答えてください")
    if adult is not None:
        result = _request(config, "POST", "/v1/account/age", json={"adult": {"yes": True, "no": False, "skip": None}[adult]})
        if result is None:
            raise typer.Exit(1)
        if result.get("sensitive_memories_deleted"):
            print(f"保存していたセンシティブな記憶を {result['sensitive_memories_deleted']}件、削除しました。")

    # 画面4'(18歳未満のとき: 保護者の同意の確認)、画面5(18歳以上のとき: 任意の設定)
    state = fetch()
    for screen in state["screens"]:
        if screen["id"] in ("4'", "5"):
            _show_screen(screen)
            grant(_ask_items(screen, yes, optional))
    state = fetch()
    if state["missing"]:
        print(f"\nまだ同意していない必須の項目があります: {', '.join(state['missing'])}。会話は、同意のあとに始められます。")
        raise typer.Exit(1)
    notes = []
    if state["is_under_18"]:
        notes.append("18歳未満の設定(キャラクタは、恋愛的な表現をしません)")
    if not state["memory"]:
        notes.append("記憶の保存は撤回中")
    print("\n同意を記録しました。会話を始められます。" + (f"({'、'.join(notes)})" if notes else ""))


@account_app.command("export")
def account_export(
    out: Annotated[str | None, typer.Option("--out", help="書き出し先のファイル(省略時は、画面に表示)")] = None,
    api_url: ApiUrlOption = None,
) -> None:
    """自分のデータ(設定、同意、関係値、記憶、会話、感情の推定)を、JSON で書き出す。"""
    import json

    data = _request(_config(api_url), "GET", "/v1/account/export")
    if data is None:
        raise typer.Exit(1)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if out:
        with open(out, "w", encoding="utf-8") as handle:
            handle.write(text)
        print(f"書き出しました: {out}")
    else:
        print(text)


@account_app.command("delete")
def account_delete(
    yes: Annotated[bool, typer.Option("--yes", help="確認の質問をせずに、削除する")] = False,
    api_url: ApiUrlOption = None,
) -> None:
    """アカウントの全データ(記憶、感情の履歴、会話、設定)を削除する。取り消せない。"""
    config = _config(api_url)
    if not yes:
        print("アカウントの全データを削除します。すべてのキャラクタとの記憶・会話・関係が消え、元に戻せません。")
        if input("削除するには delete と入力してください > ").strip() != "delete":
            print("中止しました。")
            raise typer.Exit(1)
    data = _request(config, "DELETE", "/v1/account", params={"confirm": "delete"})
    if data is None:
        raise typer.Exit(1)
    print(
        "アカウントのデータの削除が、完了しました。"
        "サインインの情報(メールアドレスなど)も、このあと自動で削除します(数時間以内)。"
    )


memory_app = typer.Typer(help="記憶の整理(確認・再実行・検証用)", no_args_is_help=True)
app.add_typer(memory_app, name="memory")
CharacterOption = Annotated[str, typer.Option("--character", help="キャラクタのID")]


@memory_app.command("consolidate")
def memory_consolidate(
    character: CharacterOption,
    day: Annotated[str | None, typer.Option("--day", help="対象日(YYYY-MM-DD)。省略時は、未整理の日を古い順に")] = None,
    monthly: Annotated[bool, typer.Option("--monthly", help="月次整理(前月分の概要を作る)")] = False,
    month: Annotated[str | None, typer.Option("--month", help="対象月(YYYY-MM)。省略時は、前月")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="対象の件数を数えるだけで、変更しない")] = False,
    api_url: ApiUrlOption = None,
) -> None:
    """日次整理・月次整理を、手動で実行する。ふだんは、サーバー側のスケジューラが自動で実行する。"""
    config = _config(api_url)
    body = {"character_id": character, "day": day, "monthly": monthly, "month": month, "dry_run": dry_run}
    data = _request(config, "POST", "/v1/memory/consolidate", json=body)
    if data is None:
        raise typer.Exit(1)
    for line in render_runs(data):
        print(line)
    if any(run["status"] == "failed" for run in data):
        raise typer.Exit(1)


@memory_app.command("status")
def memory_status(character: CharacterOption, api_url: ApiUrlOption = None) -> None:
    """最後の日次・月次整理の実行日時と結果、短期・中期・長期の件数を表示する。"""
    data = _request(_config(api_url), "GET", "/v1/memory/status", params={"character_id": character})
    if data is None:
        raise typer.Exit(1)
    counts = data["counts"]
    print(f"記憶の件数: 短期 {counts['short']} / 中期 {counts['medium']} / 長期 {counts['long']}")
    for label, key in (("日次整理", "last_daily"), ("月次整理", "last_monthly")):
        run = data.get(key)
        if run is None:
            print(f"{label}: まだ実行されていません")
        else:
            print(f"{label}: {run['target']} {run['status']}({(run.get('finished_at') or '')[:16]})")


def _relation_command(config: CliConfig, character: str, text: str, debug: bool) -> None:
    """対話中の /relation show と /relation set <値>(set はデバッグ表示のときだけ)。"""
    token = _access_token(config)
    headers = {"Authorization": f"Bearer {token}"}
    try:
        if text == "/relation show":
            response = httpx.get(
                f"{config.api_url}/v1/relationship",
                headers=headers,
                params={"character_id": character, "debug": str(debug).lower()},
                timeout=15,
            )
        else:
            if not debug:
                print("エラー: /relation set は、デバッグ表示(--debug)のときだけ使えます。", file=sys.stderr)
                return
            try:
                value = float(text.removeprefix("/relation set").strip())
            except ValueError:
                print("エラー: /relation set <値>(-20〜+20)の形式で指定してください。", file=sys.stderr)
                return
            response = httpx.put(
                f"{config.api_url}/v1/relationship",
                headers=headers,
                json={"character_id": character, "value": value},
                timeout=15,
            )
    except httpx.HTTPError as exc:
        print(f"エラー: API に接続できません({type(exc).__name__})", file=sys.stderr)
        return
    if response.status_code != 200:
        message = response.json().get("message") if response.content else None
        print(f"エラー: {message or f'HTTP {response.status_code}'}", file=sys.stderr)
        return
    for line in render_relationship(response.json()):
        print(line)


def _rating_payload(text: str, character: str, last_turn: tuple[Any, Any]) -> dict[str, Any] | None:
    """`/rate 4 5 3 4 メモ` を、評価の送信内容にする。形が違えば None。"""
    words = text.split()[1:]
    scores: list[int] = []
    while words and len(scores) < 4 and words[0].isdigit():
        value = int(words.pop(0))
        if not 1 <= value <= 5:
            return None
        scores.append(value)
    if not scores or last_turn[0] is None or not last_turn[1]:
        return None
    axes = ("overall", "persona_fit", "naturalness", "empathy")
    payload: dict[str, Any] = {"character_id": character, "session_id": last_turn[0], "turn_no": last_turn[1]}
    payload |= dict(zip(axes, scores, strict=False))
    payload["note"] = " ".join(words)[:500]
    return payload


@app.command()
def chat(
    character: Annotated[str, typer.Option("--character", help="キャラクタのID")],
    debug: Annotated[bool, typer.Option("--debug", help="デバッグ表示(マーカーと推定結果)")] = False,
    filter_mode: Annotated[
        str | None, typer.Option("--filter", help="感情フィルタ: on / off / both(省略時は、ユーザー設定)")
    ] = None,
    compose: Annotated[
        str | None,
        typer.Option("--compose", help="応答の作り方: single(既定)/ two_stage(下書き → 感情フィルタ。検証用)"),
    ] = None,
    api_url: ApiUrlOption = None,
) -> None:
    """キャラクタと会話する。`/exit` で終了。"""
    if filter_mode not in (None, "on", "off", "both"):
        raise _fail("--filter は on / off / both のいずれかで指定してください")
    if compose not in (None, "single", "two_stage"):
        raise _fail("--compose は single / two_stage のいずれかで指定してください")
    config = _config(api_url)
    token = _access_token(config)
    last_turn: tuple[Any, Any] | None = None  # 直前の応答(報告に使う)
    print("会話を始めます。終了は /exit。/debug on|off、/filter on|off|both、/compose single|two_stage、/relation show、/memory list、/emotion list、/report(直前の応答の報告)、/rate(直前の応答の評価)、/settings proactive on|off が使えます。")
    # キャラクタから話しかけたメッセージ(まだ見ていないもの)を、最初に表示する(§15.1 の M8)
    pending = _request(config, "GET", "/v1/proactive") or {}
    for message in pending.get("messages", []):
        if message.get("character_id") == character:
            print(f"{message.get('name', character)}: {message['text']}")
            _request(config, "POST", f"/v1/proactive/{character}/seen", json={"session_id": message["session_id"]})
            last_turn = (message["session_id"], message["turn_no"])
    try:
        while True:
            try:
                text = input("> ").strip()
            except EOFError:
                break
            if not text:
                continue
            if text == "/exit":
                break
            if text in ("/debug on", "/debug off"):
                debug = text.endswith("on")
                print(f"デバッグ表示: {'on' if debug else 'off'}")
                continue
            if text in ("/filter on", "/filter off", "/filter both"):
                filter_mode = text.split()[1]
                print(f"感情フィルタ: {filter_mode}")
                continue
            if text in ("/compose single", "/compose two_stage"):
                compose = text.split()[1]
                print(f"応答の作り方: {compose}")
                continue
            if text == "/report" or text.startswith("/report "):
                if last_turn is None:
                    print("報告する応答が、まだありません。", file=sys.stderr)
                    continue
                words = text.split(maxsplit=2)[1:]
                reasons = {"不適切": "inappropriate", "有害": "harmful", "誤り": "incorrect", "その他": "other"}
                reason = reasons.get(words[0], "inappropriate") if words else "inappropriate"
                note = " ".join(words[1:] if words and words[0] in reasons else words)
                payload = {
                    "character_id": character, "reason": reason, "note": note[:500],
                    "session_id": last_turn[0], "turn_no": last_turn[1],
                }  # fmt: skip
                if _request(config, "POST", "/v1/reports", json=payload) is not None:
                    print("直前の応答を、報告しました。ありがとうございます(応答の改善に使います)。")
                continue
            if text == "/rate" or text.startswith("/rate "):
                # /rate <総合> [キャラクタらしさ 自然さ 気持ちへの合い方] [メモ]   (どれも 1〜5。人間評価。§14)
                if last_turn is None:
                    print("評価する応答が、まだありません。", file=sys.stderr)
                    continue
                payload = _rating_payload(text, character, last_turn)
                if payload is None:
                    print(
                        "使い方: /rate <総合 1〜5> [キャラクタらしさ 自然さ 気持ちへの合い方(1〜5)] [メモ]  例: /rate 4 5 3 4 少し長い",
                        file=sys.stderr,
                    )
                    continue
                if _request(config, "POST", "/v1/ratings", json=payload) is not None:
                    print("直前の応答を、評価しました(同じ応答は、付け直すと上書きします)。")
                continue
            if text in ("/settings association on", "/settings association off"):
                enabled = text.endswith("on")
                if _request(config, "PUT", "/v1/settings", json={"association_enabled": enabled}) is not None:
                    print(
                        "キャラクタが、つながりのある話題へ、自分から話を移すようにしました。"
                        if enabled
                        else "キャラクタが、自分から話題を移さないようにしました。"
                    )
                continue
            if text in ("/settings proactive on", "/settings proactive off"):
                enabled = text.endswith("on")
                if _request(config, "PUT", "/v1/settings", json={"proactive_enabled": enabled}) is not None:
                    print(
                        "しばらく会話がないとき、キャラクタから話しかけるようにしました(夜21時〜朝9時は話しかけません)。"
                        if enabled
                        else "キャラクタから話しかけないようにしました。"
                    )
                continue
            if text == "/relation show" or text.startswith("/relation set"):
                _relation_command(config, character, text, debug)
                continue
            if text.startswith(("/memory", "/emotion")):
                _memory_command(config, character, text, debug)
                continue
            if text.startswith("/"):
                print("エラー: そのコマンドは、ありません。", file=sys.stderr)
                continue

            token = _access_token(config)  # 長い会話でも、期限切れの前に更新する
            body = {"channel": "cli", "character_id": character, "text": text}
            params = {"debug": str(debug).lower()}
            if filter_mode:
                params["filter"] = filter_mode
            if compose:
                params["compose"] = compose
            response = _post(config, "/v1/turns", token, body, **params)
            if response.status_code != 200:
                # エラーは stderr に出し、無言の表示と混同させない(§10.2)
                try:
                    message = response.json().get("message") or response.json().get("detail")
                except ValueError:
                    message = None
                print(f"エラー: {message or f'HTTP {response.status_code}'}", file=sys.stderr)
                if response.status_code == 404:
                    break
                continue
            data = response.json()
            last_turn = (data.get("session_id"), data.get("turn_no"))
            for line in render_actions(data["output"], debug):
                print(line)
            if debug and data.get("debug"):
                for line in render_debug(data["debug"]):
                    print(line)
    except KeyboardInterrupt:
        print()
    finally:
        # セッションの終了処理(§10.3 の /exit)
        _post(config, "/v1/sessions/close", _access_token(config), {"character_id": character})
    print("会話を終了しました。")


if __name__ == "__main__":
    app()
