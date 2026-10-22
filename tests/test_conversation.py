import unittest
from unittest.mock import patch

from helpers import FULL_BRIEF, FakeLLM, FakeStore, make_prompts
from agent.models import Session
from agent.pipeline import Pipeline


class ConversationTest(unittest.TestCase):
    def test_exact_choice_overrides_stale_model_and_preserves_brief(self):
        llm = FakeLLM({**FULL_BRIEF, "product": "MateBook X Pro"})
        store = FakeStore(models={"MateBook 14": "F14"})
        pipe = Pipeline(llm, store, make_prompts())
        s = Session()
        self.assertEqual(pipe.talk(s, "推广 MateBook X Pro").status, "need_choice")
        self.assertEqual(s.history[-1]["role"], "助手")
        out = pipe.talk(s, "HUAWEI MateBook 14")
        self.assertEqual(out.status, "review")
        self.assertEqual(s.brief.product, "MateBook 14")
        self.assertEqual(s.brief.platform, FULL_BRIEF["platform"])
        self.assertEqual(llm.calls.count("intake"), 1)

    def test_choice_with_incomplete_brief_still_asks_questions(self):
        pipe = Pipeline(FakeLLM(), FakeStore(), make_prompts())
        s = Session()
        out = pipe.talk(s, "HUAWEI MateBook 14")
        self.assertEqual(out.status, "need_more_info")
        self.assertEqual(s.brief.product, "MateBook 14")

    def test_product_change_cannot_restore_previous_model(self):
        llm = FakeLLM(FULL_BRIEF, intent={"action": "change_product", "value": "MateBook 14s"})
        pipe = Pipeline(llm, FakeStore(), make_prompts())
        s = Session()
        pipe.talk(s, "go")
        pipe.feedback(s, "改推 MateBook 14s")
        self.assertEqual(s.brief.product, "MateBook 14s")
        self.assertEqual(s.history[-1]["content"], "改推 MateBook 14s")
        self.assertIsNone(s.draft)

    def test_structured_product_change_bypasses_intent_guessing_and_regenerates(self):
        llm = FakeLLM(FULL_BRIEF)
        store = FakeStore(docs={
            "MateBook 14": "MateBook 14 重量约1.36kg。内存16GB。",
            "MateBook X Pro": "MateBook X Pro 重量约1.36kg。内存16GB。",
        })
        pipe = Pipeline(llm, store, make_prompts())
        s = Session()
        self.assertEqual(pipe.talk(s, "go").status, "review")
        out = pipe.feedback(
            s,
            "本次创作设置：产品：MateBook X Pro",
            intent_override={"action": "change_product", "value": "MateBook X Pro", "note": "界面切换"},
        )
        self.assertEqual(out.status, "review")
        self.assertEqual(s.brief.product, "MateBook X Pro")
        self.assertNotIn("revise_intent", llm.calls)

    def test_web_continues_editing_after_confirm_and_clarification(self):
        import app
        llm = FakeLLM(FULL_BRIEF)
        pipe = Pipeline(llm, FakeStore(), make_prompts())
        with patch.object(app, "get_pipeline", return_value=pipe), patch.object(pipe, "save"):
            _, history, state = app.user_send("go", [], None)
            _, history, state = app.user_send("确认", history, state)
            self.assertEqual(state["last"], "done")
            llm.intent = {"action": "unclear"}
            _, history, state = app.user_send("再改改", history, state)
            self.assertEqual(state["last"], "need_more_info")
            llm.intent = {"action": "rewrite", "note": "标题短一点"}
            _, history, state = app.user_send("标题短一点", history, state)
            self.assertEqual(state["last"], "review")
            self.assertIn("标题短一点", state["session"].brief.extra)
            self.assertEqual(llm.calls.count("intake"), 1)
            self.assertEqual(len(history), 8)
