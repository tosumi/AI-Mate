"""1ターンの遅延の測定のテスト(本体仕様書 §15.1 の M6-6)。模擬のモデルで、集計と比較の資料を確かめる。"""

from mycompanion.eval import turn_eval
from mycompanion.llm.mock import MockLLMClient


async def test_turn_eval_summarizes_and_hides_combo_names():
    personas = [p for p in turn_eval.load_personas() if p.character_id == "sample_oshaberi"]
    samples = await turn_eval.run_combo(MockLLMClient(), personas, runs=1, inputs=turn_eval.TURN_INPUTS[:3])
    summary = turn_eval.summarize(samples)
    assert summary["turns"] == 3 and set(summary["total_sec"]) == {"p50", "p95", "mean"}
    sheet, key = turn_eval.comparison_sheet({"draftA / filterA": samples, "draftB / filterB": samples})
    assert "draftA" not in sheet and "draftA" in key and turn_eval.TURN_INPUTS[0] in sheet
    assert turn_eval.render({"x / y": summary})[2].startswith("| x / y |")


async def test_single_compose_mode_skips_the_filter_and_passes_the_directive():
    import uuid

    from mycompanion.channel.types import TurnInput
    from mycompanion.config import EngineSettings
    from mycompanion.engine.engine import Engine
    from mycompanion.engine.stores import InMemoryStores

    llm = MockLLMClient()
    seen: list[str] = []
    calls = {"filter": 0}
    draft, filt = llm.draft, llm.filter

    async def spy_draft(persona, history, context=None):
        seen.append(context.directive if context else "")
        return await draft(persona, history, context)

    async def spy_filter(*args, **kwargs):
        calls["filter"] += 1
        return await filt(*args, **kwargs)

    llm.draft, llm.filter = spy_draft, spy_filter
    persona = next(p for p in turn_eval.load_personas() if p.character_id == "sample_oshaberi")
    stores = InMemoryStores()
    stores.add_character(persona)
    engine = Engine(llm, stores, EngineSettings(compose_mode="single"), None)
    user = uuid.uuid4()
    turn = TurnInput(channel="cli", character_id=persona.character_id, text="明日から旅行なんだ。すごく楽しみ!")
    await engine.handle_turn(user, turn, use_memory=False)
    (record,) = stores.turns[(user, persona.character_id)]
    # 投機実行の下書きはなく、方針つきの1回だけ。フィルタは呼ばない
    assert len(seen) == 1 and "<directive>" in seen[0] and "戦略:" in seen[0]
    # 1回で作った応答は、最終の応答(整えた応答)として記録・表示する
    assert calls["filter"] == 0 and record.draft_text and record.filtered_text == record.draft_text
