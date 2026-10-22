import unittest

from helpers import FULL_BRIEF, FakeLLM, FakeStore, good_draft, make_prompts
from agent import export
from agent.models import Session
from agent.pipeline import Pipeline


class RelaxedReviewTest(unittest.TestCase):
    def run_pipeline(self, draft=None):
        llm = FakeLLM(
            FULL_BRIEF, drafts=[draft or good_draft()],
            comp_issues=[{"where": "整体语气", "detail": "语气建议更克制"}],
        )
        session = Session()
        out = Pipeline(llm, FakeStore(), make_prompts(), review_mode="relaxed").talk(session, "go")
        return out, session, llm

    def test_advisory_does_not_block_or_trigger_rewrite(self):
        out, session, llm = self.run_pipeline()
        self.assertEqual(out.status, "review")
        self.assertNotIn("chunk_rewrite", llm.calls)
        self.assertTrue(any(i.source == "llm" and i.severity == "warn" for i in session.review.issues))
        report = export.to_markdown(session)
        self.assertIn("仍有提示待确认", report)
        self.assertIn("语气建议更克制", report)

    def test_unsupported_numbers_still_removed(self):
        draft = good_draft()
        draft["body"].append({"text": "续航长达20小时。", "fact_ids": ["F1"]})
        out, session, llm = self.run_pipeline(draft)
        self.assertEqual(out.status, "review")
        self.assertIn("chunk_rewrite", llm.calls)
        self.assertNotIn("20小时", export.to_text(session))
        self.assertTrue(session.review.removed)

    def test_plain_text_contains_only_article(self):
        _, session, _ = self.run_pipeline()
        text = export.to_text(session)
        self.assertIn(session.draft.title, text)
        self.assertIn(session.draft.body[0].text, text)
        self.assertNotIn("知识库依据", text)
        self.assertNotIn("语气建议更克制", text)


if __name__ == "__main__":
    unittest.main()
