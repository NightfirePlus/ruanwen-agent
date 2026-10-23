import time
import unittest

from helpers import FULL_BRIEF, FakeLLM, good_draft, make_prompts
from agent import briefing, chunk_pipeline, chunk_review, compose, strategy
from agent import planning
from agent.models import Brief, ContentBlock, ContentPlan, Draft, Evidence, Fact, Paragraph, Session
from agent.llm import LLMError
from agent.pipeline import Pipeline
from helpers import FakeStore


class SlowReviewer:
    def json(self, user, **kwargs):
        time.sleep(0.15)
        return {"issues": []}


class FailingFinalReview(FakeLLM):
    def json(self, user, **kwargs):
        if user.startswith("【fact_review】"):
            raise LLMError("模拟 429")
        return super().json(user, **kwargs)


class ChunkPipelineTest(unittest.TestCase):
    def setUp(self):
        self.brief = Brief(**FULL_BRIEF)
        self.evidence = Evidence(facts=[
            Fact("F1", "MateBook 14", "重量约1.36kg", "产品事实", "facts.md"),
        ])
        self.strategy = strategy.match(self.brief)

    def test_canonical_brief_is_deterministic_and_contains_no_model_notes(self):
        first = briefing.canonicalize(self.brief)
        second = briefing.canonicalize(self.brief)
        self.assertEqual(first, second)
        self.assertIn("产品：MateBook 14", first)
        self.assertIn("目标受众：大学新生", first)
        self.assertNotIn("测试", first)  # llm_notes 不进入规范任务说明

    def test_writer_and_reviewer_overlap_on_independent_blocks(self):
        plan = ContentPlan([
            ContentBlock("B1", "body", "第一卖点", ["F1"]),
            ContentBlock("B2", "body", "第二卖点", ["F1"]),
        ])
        result = chunk_pipeline.run(
            FakeLLM(drafts=[good_draft()]), SlowReviewer(), make_prompts(),
            briefing.canonicalize(self.brief), self.brief, self.evidence,
            self.strategy, plan,
        )
        self.assertFalse(result.error)
        sequence = [(item["event"], item["block"]) for item in result.events
                    if item["event"] in ("written", "reviewed")]
        # 审核 B1 尚未完成时，单写作消费者已经产出 B2，证明两条队列发生了时间重叠。
        self.assertLess(sequence.index(("written", "B2")), sequence.index(("reviewed", "B1")))
        self.assertTrue(all(block.status == "passed" for block in plan.blocks))
        self.assertGreaterEqual(result.metrics["generated_versions"], 2)
        # 安全删除后的版本会直接复审，因此审核版本数可以多于模型生成版本数。
        self.assertGreaterEqual(
            result.metrics["reviewed_versions"], result.metrics["generated_versions"]
        )
        self.assertGreater(result.metrics["first_block_passed_seconds"], 0)
        self.assertGreaterEqual(result.metrics["total_seconds"], result.metrics["first_block_passed_seconds"])

    def test_fact_lock_rejects_number_fact_id_image_and_structure_changes(self):
        original = Draft(
            title="轻装出发", body=[Paragraph("重量约1.36kg", ["F1"])], tags=["轻薄"]
        )
        changed_number = Draft(
            title="轻装出发", body=[Paragraph("重量约1.20kg", ["F1"])], tags=["轻薄"]
        )
        changed_id = Draft(
            title="轻装出发", body=[Paragraph("重量约1.36kg", ["F2"])], tags=["轻薄"]
        )
        changed_structure = Draft(
            title="轻装出发", body=[Paragraph("重量约1.36kg", ["F1"]), Paragraph("新增段", [])], tags=["轻薄"]
        )
        self.assertTrue(compose.fact_lock_violations(original, changed_number))
        self.assertTrue(compose.fact_lock_violations(original, changed_id))
        self.assertTrue(compose.fact_lock_violations(original, changed_structure))

    def test_reviewer_alias_fields_are_not_silently_dropped(self):
        issues = chunk_review.parse_llm_issues({"issues": [{
            "stage": "body", "location": "body[0].text",
            "problem": "遗漏适用条件", "fix": "补回限定条件",
        }]})
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].stage, "fact")
        self.assertEqual(issues[0].detail, "遗漏适用条件")
        self.assertEqual(issues[0].suggestion, "补回限定条件")

    def test_plan_objectives_cannot_weaken_block_structure(self):
        plan = planning.parse_plan({"blocks": [
            {"id": "B1", "kind": "opening", "objective": "一句话主文案", "fact_ids": []},
            {"id": "B2", "kind": "body", "objective": "介绍卖点", "fact_ids": ["F1"]},
            {"id": "B3", "kind": "closing", "objective": "收束", "fact_ids": []},
        ]}, self.evidence)
        self.assertIn("非空标题和非空导语", plan.blocks[0].objective)
        self.assertIn("完整的正文小节", plan.blocks[1].objective)
        self.assertIn("非空行动引导", plan.blocks[2].objective)
        self.assertEqual(plan.blocks[0].fact_ids, [])
        self.assertLessEqual(len(plan.blocks[1].fact_ids), 3)
        self.assertEqual(plan.blocks[2].fact_ids, [])

    def test_pipeline_persists_plan_versions_and_queue_trace(self):
        session = Session()
        out = Pipeline(FakeLLM(FULL_BRIEF), FakeStore(), make_prompts()).talk(session, "go")
        self.assertEqual(out.status, "review", out.reason)
        self.assertTrue(session.canonical_brief)
        self.assertTrue(session.content_plan)
        self.assertTrue(all(block.status == "passed" and block.version >= 1
                            for block in session.content_plan.blocks))
        queue_trace = next(item for item in session.trace if item["stage"] == "5 分块队列")
        self.assertTrue(queue_trace["events"])
        self.assertGreaterEqual(queue_trace["metrics"]["generated_versions"], 3)

    def test_semantic_fact_change_from_coherence_editor_is_reverted(self):
        changed = good_draft()
        changed["body"][0]["text"] = "重量约1.36kg，性能全面领先。"
        llm = FakeLLM(
            FULL_BRIEF,
            coherence=changed,
            coherence_changes={"changed": True, "issues": [
                {"where": "正文第1段", "detail": "新增了没有事实支持的性能结论"}
            ]},
        )
        session = Session()
        out = Pipeline(llm, FakeStore(), make_prompts()).talk(session, "go")
        self.assertEqual(out.status, "review", out.reason)
        self.assertNotIn("性能全面领先", session.draft.body[0].text)
        trace = next(item for item in session.trace if item["stage"] == "6 整文连贯性编辑")
        self.assertFalse(trace["applied"])
        self.assertTrue(any("性能结论" in note for note in trace["notes"]))

    def test_final_review_api_failure_blocks_instead_of_escaping(self):
        session = Session()
        out = Pipeline(FailingFinalReview(FULL_BRIEF), FakeStore(), make_prompts()).talk(session, "go")
        self.assertEqual(out.status, "blocked")
        self.assertIn("全文终审模型调用失败", out.reason)
        self.assertIsNone(session.review)


if __name__ == "__main__":
    unittest.main()
