import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import compliance, export, intake, verify  # noqa: E402
from agent.models import Draft, Fact, ImagePlacement, ImageRef, Paragraph, Task  # noqa: E402
from agent.pipeline import Pipeline  # noqa: E402
from agent.prompts import PROMPTS_DIR, SPECS, PromptError, Prompts  # noqa: E402

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
        self.systems: dict[str, object] = {}  # 环节 -> 该次调用收到的 system

    def chat_json(self, system, user, temperature=0.2):
        self.calls += 1
        if user.startswith("你要为华为PC营销内容做场景判断"):
            self.systems["scene"] = system
            return {"audience": "学生", "usage_scene": "课堂", "stage": "对比选购", "main_problem": "携带", "goal": "种草", "angle": "轻便"}
        if user.startswith("从下面的产品资料中摘录"):
            self.systems["facts"] = system
            return {"facts": [{"text": "重量约1.36kg"}, {"text": "内存16GB"}, {"text": "不在原文中的编造内容"}], "conflicts": []}
        self.systems["compose"] = system
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


def test_prompts() -> Prompts:
    """用仓库里真实的环节提示词,只给 system 补一句话(system.md 由别人填写,目前为空)。"""
    texts = {n: (PROMPTS_DIR / f"{n}.md").read_text(encoding="utf-8") for n in SPECS if n != "system"}
    texts["system"] = "你是华为PC营销软文智能体。"
    return Prompts(texts)


def pipe(llm):
    return Pipeline(llm, FakeRetriever(), test_prompts())


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
        r = pipe(llm).run(Task("轻薄本"))
        self.assertEqual(r.status, "need_more_info")
        self.assertEqual(llm.calls, 0)

    def test_happy_path_and_hallucinated_fact_dropped(self):
        r = pipe(FakeLLM([good_draft()])).run(task())
        self.assertEqual(r.status, "done", r.reason)
        self.assertEqual([f.text for f in r.facts], ["重量约1.36kg", "内存16GB"])
        # 型号不符的图片不会进入可选清单
        self.assertIn("I1", [i.image_id for i in r.draft.images])

    def test_revision_removes_bad_content(self):
        bad = good_draft()
        bad["body"] = [{"text": "续航长达20小时,国补立减1000元。", "fact_ids": ["F1"]}]
        r = pipe(FakeLLM([bad, good_draft()])).run(task())
        self.assertEqual(r.status, "done")
        self.assertNotIn("国补", r.draft.all_text())

    def test_unfixable_draft_is_blocked(self):
        bad = good_draft()
        bad["body"] = [{"text": "续航长达20小时", "fact_ids": ["F1"]}]
        r = pipe(FakeLLM([bad])).run(task())
        self.assertEqual(r.status, "blocked")

    def test_system_prompt_only_used_for_writing(self):
        llm = FakeLLM([good_draft()])
        pipe(llm).run(task())
        self.assertIsNone(llm.systems["scene"])      # 场景解析不带写作人设
        self.assertIsNone(llm.systems["facts"])      # 事实提取不带写作人设
        self.assertEqual(llm.systems["compose"], "你是华为PC营销软文智能体。")

    def test_export_has_four_sections(self):
        r = pipe(FakeLLM([good_draft()])).run(task())
        md = export.to_markdown(r)
        for h in ("# 一、场景判断", "# 二、知识库依据", "# 三、图文软文", "# 四、核验结果"):
            self.assertIn(h, md)
        self.assertNotIn("F1]", md.split("# 三、图文软文")[1].split("# 四")[0])


class PromptsTest(unittest.TestCase):
    def full(self):
        return {n: "任务说明 " + " ".join("{{%s}}" % v for v in req) + " 正文" for n, req in SPECS.items() if n != "system"} | {"system": "角色说明"}

    def test_repo_prompts_reject_blank_system(self):
        # system.md 目前只有标题,必须被识别为未填写,并且不影响其他文件被检查
        with self.assertRaises(PromptError) as ctx:
            Prompts.load()
        msg = str(ctx.exception)
        self.assertIn("system.md 还没有填写内容", msg)
        self.assertNotIn("scene.md", msg)

    def test_missing_file_reported(self):
        t = self.full(); del t["facts"]
        with self.assertRaises(PromptError) as ctx:
            Prompts(t)
        self.assertIn("facts.md 不存在", str(ctx.exception))

    def test_all_problems_reported_together(self):
        t = self.full(); t["scene"] = "# 只有标题"; del t["revise"]
        with self.assertRaises(PromptError) as ctx:
            Prompts(t)
        msg = str(ctx.exception)
        self.assertIn("scene.md", msg); self.assertIn("revise.md", msg)

    def test_missing_variable_reported(self):
        t = self.full(); t["facts"] = "没有任何变量"
        with self.assertRaises(PromptError) as ctx:
            Prompts(t)
        self.assertIn("缺少变量", str(ctx.exception))

    def test_unknown_variable_reported(self):
        t = self.full(); t["facts"] += " {{拼错了的变量}}"
        with self.assertRaises(PromptError) as ctx:
            Prompts(t)
        self.assertIn("未定义的变量", str(ctx.exception))

    def test_comments_stripped(self):
        t = self.full(); t["system"] = "<!-- 给编写者看的 -->\n你是Agent"
        self.assertEqual(Prompts(t).system, "你是Agent")

    def test_render_does_not_expand_values(self):
        out = Prompts(self.full()).render("facts", {"型号": "{{资料标题}}", "资料标题": "T", "资料全文": "x"})
        self.assertIn("{{资料标题}}", out)  # 值里的花括号原样保留,不被二次展开

    def test_render_requires_all_values(self):
        with self.assertRaises(PromptError):
            Prompts(self.full()).render("facts", {"型号": "a"})


if __name__ == "__main__":
    unittest.main()
