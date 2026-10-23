import unittest

from agent.models import Session
from agent.pipeline import Pipeline
from helpers import FULL_BRIEF, FakeLLM, FakeStore, make_prompts


class ContextPackageTest(unittest.TestCase):
    def test_preferences_and_history_are_advisory_not_fact_evidence(self):
        performance = {"mode": "advisory_weight", "references": [{
            "title": "历史高表现稿", "reference_weight": 0.8,
        }]}
        preferences = {"preferred_tone": "专业真诚", "avoid_topics": "未经确认的价格"}
        llm = FakeLLM(FULL_BRIEF)
        pipe = Pipeline(
            llm, FakeStore(), make_prompts(), review_mode="relaxed",
            performance_provider=lambda brief: performance,
            preference_provider=lambda: preferences,
        )
        out = pipe.talk(Session(), "写一篇开学季软文")
        self.assertEqual(out.status, "review", out.reason)
        package = out.session.context_package
        self.assertEqual(package.contract_version, "writing-context-v1")
        self.assertEqual(package.user_preferences["preferred_tone"], "专业真诚")
        self.assertEqual(package.performance_references[0]["title"], "历史高表现稿")
        self.assertIn("唯一可用于", package.authority["facts"])
        self.assertEqual([fact.text for fact in package.facts], ["重量约1.36kg", "内存16GB"])
        self.assertIn("专业真诚", llm.users["content_plan"])
        self.assertNotIn("专业真诚", out.session.canonical_brief)

    def test_context_package_roundtrips_in_checkpoint(self):
        llm = FakeLLM(FULL_BRIEF)
        out = Pipeline(
            llm, FakeStore(), make_prompts(), review_mode="relaxed",
            preference_provider=lambda: {"preferred_tone": "克制"},
        ).talk(Session(), "写一篇")
        restored = Session.from_dict(out.session.to_dict())
        self.assertEqual(restored.context_package.user_preferences["preferred_tone"], "克制")
        self.assertEqual(restored.context_package.facts[0].id, "F1")


if __name__ == "__main__":
    unittest.main()
