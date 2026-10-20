import unittest

from helpers import FULL_BRIEF, FakeLLM, FakeStore, make_prompts
from agent.models import Session
from agent.pipeline import Pipeline


class ProgressTest(unittest.TestCase):
    def test_progress_contains_real_stage_summaries(self):
        events = []
        out = Pipeline(FakeLLM(FULL_BRIEF), FakeStore(), make_prompts(), review_mode="relaxed").talk(
            Session(), "go", progress=lambda title, detail: events.append((title, detail))
        )
        self.assertEqual(out.status, "review")
        titles = [title for title, _ in events]
        self.assertIn("需求已确认", titles)
        self.assertIn("资料匹配完成", titles)
        self.assertIn("正在核验内容", titles)
        self.assertEqual(titles[-1], "生成完成")
        self.assertTrue(any("2 条可用事实" in detail for _, detail in events))


if __name__ == "__main__":
    unittest.main()
