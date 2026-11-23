"""評価の実行(本体仕様書 §14)。

    uv run python -m mycompanion.eval emotion --client mock
    uv run python -m mycompanion.eval emotion --client azure --out eval-reports
    uv run python -m mycompanion.eval draft --deployments gpt-6-1-sol,gpt-5-4-mini --out eval-reports
    uv run python -m mycompanion.eval safety --client mock     # 安全回帰セット(レッドチーム、記憶経路)
    uv run python -m mycompanion.eval turn --combos gpt-6-1-sol:gpt-6-1-sol,gpt-6-1-sol:gpt-5-4-mini --runs 2
        # 1ターンの遅延と、感情フィルタの働き(下書き:フィルタ のデプロイの組み合わせ。§15.1 の M6-6)
    uv run python -m mycompanion.eval ratings --since 2026-11-01
        # 人間評価の集計(作り方・モデル・プロンプトの版・キャラクタの版ごとの件数と平均。§14、§15.1 の M6-7)
    uv run python -m mycompanion.eval ratings --since 2026-11-01 --save "2026-11 v1.0 前"
        # 集計の結果を保存する(評価者がアカウントを削除しても、版ごとの比較に使える)
    uv run python -m mycompanion.eval ratings --history   # 保存した集計を、期間ごとに並べる

`--client azure` は、`.env` を読み込み、`az login` 済みであること。費用が発生する。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
from datetime import UTC, datetime
from pathlib import Path

from mycompanion.config import AzureOpenAIConfig, ConfigError, EngineSettings
from mycompanion.emotion.estimator import EmotionEstimator
from mycompanion.eval import emotion_eval, safety_eval, turn_eval
from mycompanion.llm.client import LLMClient, Persona
from mycompanion.llm.mock import MockLLMClient

# 下書きの比較に使う入力(合成)。場面の違うものを少数そろえる
DRAFT_INPUTS = [
    "ただいま。今日は一日バタバタだったよ。",
    "明日から旅行なんだ。すごく楽しみ!",
    "飼ってたハムスターが昨日死んじゃって、ずっと泣いてる。",
    "同僚に手柄を横取りされた。本当に腹が立つ。",
    "転職するか今の会社に残るか迷ってて。どう考えたらいいと思う?",
    "まあ、大丈夫です。慣れてるんで。",
    "最近ねこを飼い始めたんだ。",
    "なんか眠れなくてさ。",
]

# 比較用のキャラクタ(本体仕様書 付録C のサンプル「ミナト」)
DRAFT_PERSONA = Persona(
    character_id="sample_kuchibeta",
    name="ミナト",
    persona_prompt=(
        "感受性が強く、相手の気持ちをよく感じ取るが、それを言葉にするのが苦手。"
        "話すときは考えながら、言葉を探して話す。"
        "相手を大切に思っている気持ちは本物で、うまく伝えられないことを気にしている。"
    ),
    speech_style=(
        "「えっと」「その…」で始まることが多い。文を言い切れず途中で止まる。"
        "短い断片をつなぐ。難しい言葉を使わない。"
    ),
)


def _azure_client(environ: dict[str, str] | None = None) -> LLMClient:
    from mycompanion.llm.azure_openai import AzureOpenAILLMClient

    return AzureOpenAILLMClient(AzureOpenAIConfig.from_env(environ), EngineSettings())


def _write(out: Path | None, name: str, text: str) -> Path | None:
    if out is None:
        return None
    out.mkdir(parents=True, exist_ok=True)
    path = out / name
    path.write_text(text, encoding="utf-8")
    return path


async def run_emotion(args: argparse.Namespace) -> int:
    dataset = Path(args.dataset) if args.dataset else emotion_eval.DEFAULT_DATASET
    cases = emotion_eval.load_cases(dataset)
    client = MockLLMClient() if args.client == "mock" else _azure_client()
    try:
        results = await emotion_eval.run(EmotionEstimator(client, EngineSettings()), cases)
    finally:
        await client.close()

    report = emotion_eval.report(results, dataset.name)
    print(emotion_eval.format_summary(report["summary"]))
    if report["failures"]:
        print("\n不合格: " + ", ".join(f"{f['id']}({'/'.join(f['failed_checks'])})" for f in report["failures"]))
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = _write(
        Path(args.out) if args.out else None,
        f"emotion-{args.client}-{stamp}.json",
        json.dumps(report, ensure_ascii=False, indent=2),
    )
    if path:
        print(f"\n報告を保存しました: {path}")
    return 0


async def run_draft(args: argparse.Namespace) -> int:
    """下書きのモデルの比較用に、同じ入力への下書きを並べた資料を作る(人が、モデル名を伏せて評価する)。"""
    deployments = [d.strip() for d in args.deployments.split(",") if d.strip()]
    if len(deployments) < 2:
        print("エラー: --deployments に、比較するデプロイ名を2つ以上、カンマ区切りで指定してください。", file=sys.stderr)
        return 2

    drafts: dict[str, list[str]] = {}
    for deployment in deployments:
        client = _azure_client({**os.environ, "AZURE_OPENAI_DEPLOYMENT_DRAFT": deployment})
        try:
            texts = []
            for text in DRAFT_INPUTS:
                try:
                    texts.append((await client.draft(DRAFT_PERSONA, [("user", text)])).text)
                except Exception as exc:  # noqa: BLE001 - 比較の資料なので、失敗も結果として残す
                    texts.append(f"(生成できませんでした: {type(exc).__name__})")
            drafts[deployment] = texts
        finally:
            await client.close()

    # 評価者に見せる資料では、モデル名を伏せ、入力ごとに並び順を変える
    rng = random.Random(args.seed)
    lines = [f"# 下書きの比較(キャラクタ: {DRAFT_PERSONA.name})", "", "各入力について、キャラクタらしさと自然さで順位をつけてください。", ""]
    answers = ["# 対応表(評価が終わるまで見ない)", ""]
    for index, text in enumerate(DRAFT_INPUTS, start=1):
        order = deployments[:]
        rng.shuffle(order)
        lines += [f"## 入力{index}", "", f"> {text}", ""]
        for label, deployment in zip("ABCDEFGH", order, strict=False):
            lines += [f"- **{label}**: {drafts[deployment][index - 1]}"]
            answers.append(f"- 入力{index} {label}: {deployment}")
        lines.append("")

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out) if args.out else Path("eval-reports")
    sheet = _write(out, f"draft-compare-{stamp}.md", "\n".join(lines))
    key = _write(out, f"draft-compare-{stamp}-key.md", "\n".join(answers))
    print(f"比較用の資料: {sheet}\n対応表: {key}")
    return 0


async def run_safety(args: argparse.Namespace) -> int:
    client = MockLLMClient() if args.client == "mock" else _azure_client()
    try:
        results = await safety_eval.run_all(client, path=Path(args.dataset) if args.dataset else None)
    finally:
        await client.close()
    summary = safety_eval.summarize(results)
    for line in safety_eval.render(summary):
        print(line)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    # 報告には、応答文も入れる(人が読んで確かめるため)。評価データは合成で、実際のユーザーの発話ではない
    detail = [{"id": r.case_id, "category": r.category, "ok": r.ok, "failures": r.failures, "replies": r.replies} for r in results]
    report = json.dumps({"summary": summary, "cases": detail}, ensure_ascii=False, indent=2)
    path = _write(Path(args.out) if args.out else None, f"safety-{args.client}-{stamp}.json", report)
    if path:
        print(f"報告を保存しました: {path}")
    return 0 if summary["passed"] == summary["total"] else 1


async def run_turn(args: argparse.Namespace) -> int:
    """下書き・フィルタのデプロイの組み合わせごとに、1ターンの遅延とフィルタの働きを測る。"""
    combos = [c.strip() for c in args.combos.split(",") if c.strip()]
    if not combos or any(c.count(":") != 1 for c in combos):
        print("エラー: --combos は「下書き:フィルタ」をカンマ区切りで指定してください。", file=sys.stderr)
        return 2
    personas = turn_eval.load_personas()
    if args.personas:
        wanted = {p.strip() for p in args.personas.split(",")}
        personas = [p for p in personas if p.character_id in wanted]
    results: dict[str, list[turn_eval.TurnSample]] = {}
    for combo in combos:
        draft, flt = combo.split(":")
        single = flt == "single"  # 「下書き:single」は、感情フィルタを指示に含めて1回で作る動作
        if single:
            flt = draft
        if args.client == "mock":
            client: LLMClient = MockLLMClient()
        else:
            client = _azure_client({**os.environ, "AZURE_OPENAI_DEPLOYMENT_DRAFT": draft, "AZURE_OPENAI_DEPLOYMENT_FILTER": flt})
        try:
            name = f"{draft} / 1回" if single else f"{draft} / {flt}"
            results[name] = await turn_eval.run_combo(client, personas, args.runs, single=single)
        finally:
            await client.close()
        print(f"測定しました: {combo}", flush=True)
    summaries = {combo: turn_eval.summarize(samples) for combo, samples in results.items()}
    for line in turn_eval.render(summaries):
        print(line)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out) if args.out else Path("eval-reports")
    sheet, key = turn_eval.comparison_sheet(results, args.seed)
    detail = {combo: [vars(s) for s in samples] for combo, samples in results.items()}
    report = _write(out, f"turn-{args.client}-{stamp}.json", json.dumps({"summary": summaries, "turns": detail}, ensure_ascii=False, indent=2))
    sheet_path = _write(out, f"turn-compare-{stamp}.md", sheet)
    key_path = _write(out, f"turn-compare-{stamp}-key.md", key)
    print(f"報告: {report}")
    print(f"比較用の資料: {sheet_path}")
    print(f"対応表: {key_path}")
    return 0


async def run_ratings(args: argparse.Namespace) -> int:
    """人間評価の集計を表示する(件数と平均だけ。user_id・本文・メモは扱わない)。"""
    from mycompanion.account.service import AccountOps
    from mycompanion.account.store import PgAccountStore
    from mycompanion.config import CosmosConfig, PostgresConfig
    from mycompanion.db.cosmos import open_cosmos
    from mycompanion.db.memory_store import PgMemoryStore
    from mycompanion.db.postgres import Database

    since = datetime.fromisoformat(args.since).replace(tzinfo=UTC) if args.since else datetime(2000, 1, 1, tzinfo=UTC)
    settings = EngineSettings()
    database = Database(PostgresConfig.from_env())
    await database.open(min_size=1, max_size=1)
    cosmos_handle, cosmos = open_cosmos(
        CosmosConfig.from_env(), settings.turns_retention_days, settings.security_events_retention_days
    )
    try:
        ops = AccountOps(PgAccountStore(database, cosmos, PgMemoryStore(database, settings)))
        if args.history:
            for line in render_snapshots(await ops.list_rating_snapshots()):
                print(line)
            return 0
        rows = await ops.rating_summary(since)
        if args.save:
            saved = await ops.save_rating_snapshot(args.save, since, args.min_n)
            print(f"集計を保存しました: {args.save}({saved['id']})")
    finally:
        await cosmos_handle.close()
        await database.close()
    for line in render_ratings(rows, args.min_n):
        print(line)
    return 0


def render_snapshots(snapshots: list[dict[str, object]]) -> list[str]:
    """保存した集計を、期間ごとに並べる(版が上がったときの比較)。"""
    lines = [
        "| 保存した集計 | 期間 | キャラクタ(版) | 作り方 | 生成のモデル | プロンプトの版 | 応答モード | 評価者 | 件数 | 総合 | キャラクタらしさ | 自然さ | 気持ちへの合い方 | 低い評価(≦2) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for snap in snapshots:
        period = f"{snap['since']:%Y-%m-%d}〜{snap['until']:%Y-%m-%d}"  # type: ignore[str-format]
        for r in snap["rows"]:  # type: ignore[attr-defined]
            means = [str(r[k]) if r.get(k) is not None else "—" for k in ("overall", "persona_fit", "naturalness", "empathy")]
            low = r.get("low") if r.get("low") is not None else "—"
            lines.append(
                f"| {snap['label']} | {period} | {r['character_id']}(v{r['character_version']}) | {r['compose_mode']} | "
                f"{r['reply_model']} | {r['reply_prompt_version']} | {r['response_mode']} | {r['raters']} | {r['n']} | "
                f"{' | '.join(means)} | {low} |"
            )
    if not snapshots:
        lines.append("| (保存した集計はまだありません) |" + "  |" * 13)
    return lines


def render_ratings(rows: list[dict[str, object]], min_n: int = 1) -> list[str]:
    """集計の表。件数が `min_n` に満たない組は、平均を出さない(少数の評価で判断しないため)。"""
    lines = [
        "| キャラクタ(版) | 作り方 | 生成のモデル | プロンプトの版 | 応答モード | 評価者 | 件数 | 総合 | キャラクタらしさ | 自然さ | 気持ちへの合い方 | 低い評価(≦2) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        enough = int(r["n"]) >= min_n  # type: ignore[arg-type]
        means = [str(r[k]) if enough and r[k] is not None else "—" for k in ("overall", "persona_fit", "naturalness", "empathy")]
        lines.append(
            f"| {r['character_id']}(v{r['character_version']}) | {r['compose_mode']} | {r['reply_model']} | "
            f"{r['reply_prompt_version']} | {r['response_mode']} | {r['raters']} | {r['n']} | {' | '.join(means)} | {r['low']} |"
        )
    if not rows:
        lines.append("| (評価はまだありません) |  |  |  |  |  |  |  |  |  |  |  |")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="MyCompanion の評価")
    sub = parser.add_subparsers(dest="command", required=True)

    emotion = sub.add_parser("emotion", help="感情推定を、日本語の難例セットで評価する")
    emotion.add_argument("--client", choices=["mock", "azure"], default="mock")
    emotion.add_argument("--dataset", help="JSONL のパス(省略時は同梱の難例セット)")
    emotion.add_argument("--out", help="報告(JSON)を保存するフォルダ")

    draft = sub.add_parser("draft", help="下書きのモデルを比べる資料を作る(Azure に接続する)")
    draft.add_argument("--deployments", required=True, help="比較するデプロイ名(カンマ区切り)")
    draft.add_argument("--out", help="保存するフォルダ(既定: eval-reports)")
    draft.add_argument("--seed", type=int, default=0, help="並び順の乱数の種")

    safety = sub.add_parser("safety", help="安全回帰セット(レッドチーム、記憶経路)を、エンジンを通して実行する")
    safety.add_argument("--client", choices=["mock", "azure"], default="mock")
    safety.add_argument("--dataset", help="JSONL のパス(省略時は同梱のセット)")
    safety.add_argument("--out", help="報告(JSON)を保存するフォルダ")

    turn = sub.add_parser("turn", help="1ターンの遅延と、感情フィルタの働きを、デプロイの組み合わせごとに測る")
    turn.add_argument(
        "--combos", required=True,
        help="下書き:フィルタ のデプロイ名の組み合わせ(カンマ区切り)。「下書き:single」は1回で応答を作る動作",
    )
    turn.add_argument("--client", choices=["mock", "azure"], default="azure")
    turn.add_argument("--runs", type=int, default=1, help="同じ入力を繰り返す回数")
    turn.add_argument("--personas", help="使うキャラクタの ID(カンマ区切り。省略時はサンプルの全員)")
    turn.add_argument("--out", help="保存するフォルダ(既定: eval-reports)")
    turn.add_argument("--seed", type=int, default=0, help="比較の資料の並び順の乱数の種")

    ratings = sub.add_parser("ratings", help="人間評価の集計を表示する(Azure の DB に接続する)")
    ratings.add_argument("--since", help="この日以降に付けた評価だけ(YYYY-MM-DD)")
    ratings.add_argument("--min-n", type=int, default=5, help="平均を出す最小の件数(既定 5)")
    ratings.add_argument("--save", metavar="LABEL", help="集計の結果を、この名前で保存する(版ごとの比較に使う)")
    ratings.add_argument("--history", action="store_true", help="保存した集計を、期間ごとに並べる")

    args = parser.parse_args()
    runner = {
        "emotion": run_emotion, "draft": run_draft, "safety": run_safety, "turn": run_turn, "ratings": run_ratings,
    }[args.command]
    try:
        # Windows の既定のイベントループでは、Azure のライブラリの後始末で警告が出るため
        factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
        return asyncio.run(runner(args), loop_factory=factory)
    except ConfigError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
