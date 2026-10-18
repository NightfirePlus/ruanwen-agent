import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import compliance, export, intake, verify  # noqa: E402
from agent.models import Draft, Fact, ImagePlacement, ImageRef, Paragraph, Task  # noqa: E402
from agent.pipeline import Pipeline  # noqa: E402

DOC = "MateBook 14 重量约1.36kg。内存16GB。屏幕14.2英寸。"


class FakeRetriever:
    kb_id = "KB"

    def connect(self):
        return {"connected": True, "kb_name": "x"}

    def find_docs(self, model):
        return [{"media_id": "M1", "title": "MateBook 14 参数", "folder_id": "F"}]

    def read_doc(self, media_id):
        return DOC

    def find_images(self, model):
        return [
            ImageRef("I1", "MateBook 14", "mb14-主图.png", "P1", "kb/mb14-主图.png"),
            ImageRef("I2", "MateBook X Pro", "xpro-主图.png", "P2", "kb/xpro-主图.png"),
        ]


class FakeLLM:
    """按提示词内容返回预设 JSON,并记录调用次数。"""

    def __init__(self, drafts):
        self.drafts = list(drafts)
        self.calls = 0

    def chat_json(self, system, user, temperature=0.2):
        self.calls += 1
        if "场景判断" in system:
            return {"audience": "学生", "usage_scene": "课堂", "stage": "对比选购", "main_problem": "携带", "goal": "种草", "angle": "轻便"}
        if "摘录" in system:
            return {"facts": [{"text": "重量约1.36kg"}, {"text": "内存16GB"}, {"text": "不在原文中的编造内容"}], "conflicts": []}
        return self.drafts.pop(0) if len(self.drafts) > 1 else self.drafts[0]


def good_draft():
    return {
        "title": "新生开学带它够轻",
        "intro": "每天背电脑去教室。",
        "body": [{"text": "重量约1.36kg,背着不累。", "fact_ids": ["F1"]}],
        "cta": "欢迎到店体验。",
        "tags": [],
        "images": [{"image_id": "I1", "after_paragraph": 0, "caption": "主图", "reason": "开篇"}],
        "refusals": [],
    }


def task(**kw):
    base = dict(product="MateBook 14", audience="大学新生", scene="开学季", platform="朋友圈")
    base.update(kw)
    return Task(**base)


class IntakeTest(unittest.TestCase):
    def test_missing_fields_asks(self):
        self.assertTrue(intake.check(Task(product="轻薄本")))

    def test_vague_product_asks(self):
        qs = intake.check(Task("轻薄本", "学生", "开学季", "小红书"))
        self.assertTrue(any("不够具体" in q for q in qs))

    def test_unknown_platform_asks(self):
        qs = intake.check(Task("MateBook 14", "学生", "开学季", "抖音"))
        self.assertTrue(any("暂不支持" in q for q in qs))

    def test_complete_passes(self):
        self.assertEqual(intake.check(task()), [])


class VerifyTest(unittest.TestCase):
    facts = [Fact("F1", "MateBook 14", "重量约1.36kg", "t"), Fact("F2", "MateBook 14", "内存16GB", "t")]
    images = [ImageRef("I1", "MateBook 14", "a", "m", "s"), ImageRef("I2", "MateBook X Pro", "b", "m", "s")]

    def test_fabricated_number_flagged(self):
        d = Draft(body=[Paragraph("续航长达20小时", ["F1"])])
        issues = verify.check(d, self.facts, self.images, "MateBook 14", {"min": 0, "max": 3})
        self.assertTrue(any(i.severity == "error" and "20小时" in i.detail.replace(" ", "") for i in issues))

    def test_grounded_number_passes(self):
        d = Draft(body=[Paragraph("重量约1.36kg", ["F1"])])
        issues = verify.check(d, self.facts, self.images, "MateBook 14", {"min": 0, "max": 3})
        self.assertFalse([i for i in issues if i.severity == "error"])

    def test_wrong_model_image_flagged(self):
        d = Draft(body=[Paragraph("x", [])], images=[ImagePlacement("I2", 0, "图")])
        issues = verify.check(d, self.facts, self.images, "MateBook 14", {"min": 0, "max": 3})
        self.assertTrue(any(i.kind == "image" and i.severity == "error" for i in issues))

    def test_unknown_image_flagged(self):
        d = Draft(body=[Paragraph("x", [])], images=[ImagePlacement("I9", 0, "图")])
        issues = verify.check(d, self.facts, self.images, "MateBook 14", {"min": 0, "max": 3})
        self.assertTrue(any("不在知识库图片清单" in i.detail for i in issues))

    def test_too_many_images_flagged(self):
        d = Draft(body=[Paragraph("x", [])], images=[ImagePlacement("I1", 0, "a")] * 3)
        issues = verify.check(d, self.facts, self.images, "MateBook 14", {"min": 1, "max": 1})
        self.assertTrue(any(i.kind == "platform" and i.severity == "error" for i in issues))

    def test_conflict_detection(self):
        fs = [Fact("F1", "MateBook 14", "重量约1.36kg", "t"), Fact("F2", "MateBook 14", "重量约1.40kg", "t")]
        self.assertTrue(verify.detect_conflicts(fs))


class ComplianceTest(unittest.TestCase):
    facts = [Fact("F1", "MateBook 14", "重量约1.36kg", "t")]

    def run_check(self, text):
        return compliance.check(Draft(body=[Paragraph(text)]), self.facts)

    def test_absolute(self):
        self.assertTrue(self.run_check("这是最好的选择"))

    def test_price_without_basis(self):
        self.assertTrue(any("价格" in i.detail for i in self.run_check("国补后立减1000元")))

    def test_competitor(self):
        self.assertTrue(self.run_check("性能吊打苹果"))

    def test_fake_experience(self):
        self.assertTrue(self.run_check("我亲测续航很强"))

    def test_clean_text_passes(self):
        self.assertEqual(self.run_check("适合每天通勤的同学,重量约1.36kg"), [])


class PipelineTest(unittest.TestCase):
    def test_need_more_info_makes_no_llm_call(self):
        llm = FakeLLM([good_draft()])
        r = Pipeline(llm, FakeRetriever()).run(Task("轻薄本"))
        self.assertEqual(r.status, "need_more_info")
        self.assertEqual(llm.calls, 0)

    def test_happy_path_and_hallucinated_fact_dropped(self):
        r = Pipeline(FakeLLM([good_draft()]), FakeRetriever()).run(task())
        self.assertEqual(r.status, "done", r.reason)
        self.assertEqual([f.text for f in r.facts], ["重量约1.36kg", "内存16GB"])
        # 型号不符的图片不会进入可选清单
        self.assertIn("I1", [i.image_id for i in r.draft.images])

    def test_revision_removes_bad_content(self):
        bad = good_draft()
        bad["body"] = [{"text": "续航长达20小时,国补立减1000元。", "fact_ids": ["F1"]}]
        r = Pipeline(FakeLLM([bad, good_draft()]), FakeRetriever()).run(task())
        self.assertEqual(r.status, "done")
        self.assertNotIn("国补", r.draft.all_text())

    def test_unfixable_draft_is_blocked(self):
        bad = good_draft()
        bad["body"] = [{"text": "续航长达20小时", "fact_ids": ["F1"]}]
        r = Pipeline(FakeLLM([bad]), FakeRetriever()).run(task())
        self.assertEqual(r.status, "blocked")

    def test_export_has_four_sections(self):
        r = Pipeline(FakeLLM([good_draft()]), FakeRetriever()).run(task())
        md = export.to_markdown(r)
        for h in ("# 一、场景判断", "# 二、知识库依据", "# 三、图文软文", "# 四、核验结果"):
            self.assertIn(h, md)
        self.assertNotIn("F1]", md.split("# 三、图文软文")[1].split("# 四")[0])


if __name__ == "__main__":
    unittest.main()
