import unittest

from helpers import FULL_BRIEF, FakeLLM, good_draft, make_prompts  # noqa: F401

from agent import compose, norm, prune  # noqa: E402
from agent.models import Draft, Evidence, ImagePlacement, ImageRef, Issue, Paragraph, Session  # noqa: E402
from agent.pipeline import Pipeline  # noqa: E402
from helpers import FakeStore  # noqa: E402


def err(where, detail="问题", ref=""):
    return Issue("fact", "rule", "error", where, detail, "", ref)


def draft(*texts, images=()):
    return Draft(title="t", intro="i", body=[Paragraph(x, ["F1"]) for x in texts], cta="c",
                 images=[ImagePlacement(i, n, "说明") for i, n in images])


class NormalizeWhereTest(unittest.TestCase):
    def test_variants(self):
        n = norm.normalize_where
        self.assertEqual(n("body[0].text"), "正文第1段")
        self.assertEqual(n("body[2]"), "正文第3段")
        self.assertEqual(n("正文第2段"), "正文第2段")
        self.assertEqual(n("第3段"), "正文第3段")
        self.assertEqual(n("title"), "标题")
        self.assertEqual(n("cta"), "行动引导")
        self.assertEqual(n("images[1]"), "配图方案")
        self.assertEqual(n("看不懂的位置"), "看不懂的位置")


class PruneTest(unittest.TestCase):
    def test_removes_only_bad_paragraph_and_shifts_images(self):
        d = draft("甲", "乙", "丙", images=[("I1", 0), ("I2", 2), ("I3", 3)])
        new, removed, unresolved = prune.apply(d, [err("正文第2段", "夸大")], 9)
        self.assertEqual([p.text for p in new.body], ["甲", "丙"])
        self.assertEqual([(i.image_id, i.after_paragraph) for i in new.images], [("I1", 0), ("I2", 1), ("I3", 2)])
        self.assertEqual(removed[0]["where"], "正文第2段")
        self.assertEqual(removed[0]["text"], "乙")
        self.assertEqual(unresolved, [])

    def test_original_draft_untouched(self):
        d = draft("甲", "乙")
        prune.apply(d, [err("正文第1段")], 9)
        self.assertEqual(len(d.body), 2)

    def test_multiple_paragraphs_deleted_correctly(self):
        d = draft("甲", "乙", "丙", "丁")
        new, _, _ = prune.apply(d, [err("正文第1段"), err("正文第3段")], 9)
        self.assertEqual([p.text for p in new.body], ["乙", "丁"])

    def test_intro_cta_tags(self):
        d = draft("甲")
        d.tags = ["a"]
        new, removed, _ = prune.apply(d, [err("导语"), err("行动引导"), err("标签")], 9)
        self.assertEqual((new.intro, new.cta, new.tags), ("", "", []))
        self.assertEqual({r["where"] for r in removed}, {"导语", "行动引导", "标签"})

    def test_bad_image_removed(self):
        d = draft("甲", images=[("I1", 0), ("I2", 1)])
        new, removed, _ = prune.apply(d, [err("配图方案", "型号不符", ref="I2")], 9)
        self.assertEqual([i.image_id for i in new.images], ["I1"])
        self.assertEqual(removed[0]["where"], "配图 I2")

    def test_excess_images_trimmed(self):
        d = draft("甲", images=[("I1", 0), ("I2", 0), ("I3", 1)])
        new, removed, _ = prune.apply(d, [err("配图方案", "超限", ref="count")], 2)
        self.assertEqual([i.image_id for i in new.images], ["I1", "I2"])
        self.assertEqual(len(removed), 1)

    def test_unlocatable_error_means_no_change(self):
        d = draft("甲", "乙")
        new, removed, unresolved = prune.apply(d, [err("正文第1段"), err("标题", "标题有问题")], 9)
        self.assertEqual(len(new.body), 2)          # 有一条定位不了,整体不动
        self.assertEqual(removed, [])
        self.assertEqual(len(unresolved), 1)

    def test_out_of_range_paragraph_unresolved(self):
        _, _, unresolved = prune.apply(draft("甲"), [err("正文第9段")], 9)
        self.assertEqual(len(unresolved), 1)


class CaptionFallbackTest(unittest.TestCase):
    def test_registered_caption_overrides_model_wording(self):
        ev = Evidence(images=[ImageRef("I1", "m", "a", "x", "p", caption="官方说明"), ImageRef("I2", "m", "b", "y", "p", caption="另一说明")])
        d = Draft(images=[ImagePlacement("I1", 0, ""), ImagePlacement("I2", 0, "模型自己写的")])
        compose.fill_captions(d, ev)
        self.assertEqual([i.caption for i in d.images], ["官方说明", "另一说明"])

    def test_no_registered_caption_keeps_model_caption(self):
        ev = Evidence(images=[ImageRef("I1", "m", "a", "x", "p")])
        d = Draft(images=[ImagePlacement("I1", 0, "模型写的")])
        compose.fill_captions(d, ev)
        self.assertEqual(d.images[0].caption, "模型写的")


class PipelinePruneTest(unittest.TestCase):
    def run_with(self, drafts, **kw):
        s = Session()
        out = Pipeline(FakeLLM(FULL_BRIEF, drafts=drafts, **kw), FakeStore(), make_prompts()).talk(s, "go")
        return out, s

    def bad_two_paragraphs(self):
        d = good_draft()
        d["body"] = [
            {"text": "重量约1.36kg,背着不累。", "fact_ids": ["F1"]},
            {"text": "续航长达20小时。", "fact_ids": ["F1"]},          # 数值无依据:规则 error
        ]
        return d

    def test_bad_paragraph_removed_instead_of_blocking(self):
        out, s = self.run_with([self.bad_two_paragraphs()])
        self.assertEqual(out.status, "review", out.reason)
        self.assertEqual([p.text for p in s.draft.body], ["重量约1.36kg,背着不累。"])
        self.assertEqual(s.review.removed[0]["where"], "正文第2段")
        self.assertIn("20小时", s.review.removed[0]["text"])
        self.assertTrue(s.review.passed)

    def test_removed_content_shown_in_export(self):
        from agent import export
        out, s = self.run_with([self.bad_two_paragraphs()])
        md = export.to_markdown(s)
        self.assertIn("已自动删除", md)
        self.assertNotIn("续航长达20小时。\n", md.split("# 三、图文软文")[1].split("# 四")[0])

    def test_blocked_when_body_becomes_empty(self):
        d = good_draft()
        d["body"] = [{"text": "续航长达20小时。", "fact_ids": ["F1"]}]
        out, _ = self.run_with([d])
        self.assertEqual(out.status, "blocked")
        self.assertIn("正文已为空", out.reason)

    def test_unlocatable_error_blocks_without_deleting(self):
        d = good_draft()
        d["body"] = [{"text": "重量约1.36kg", "fact_ids": ["F1"]}, {"text": "内存16GB", "fact_ids": ["F2"]}]
        llm_err = [{"where": "整体语气", "severity": "error", "detail": "整体偏夸张"}]
        out, s = self.run_with([d], comp_issues=llm_err)
        self.assertEqual(out.status, "blocked")
        self.assertEqual(len(s.draft.body), 2)                      # 定位不了就不删
        self.assertEqual(s.review.removed, [])

    def test_llm_body_index_notation_is_located(self):
        d = good_draft()
        d["body"] = [{"text": "重量约1.36kg", "fact_ids": ["F1"]}, {"text": "内存16GB", "fact_ids": ["F2"]}]
        llm_err = [{"where": "body[1].text", "severity": "error", "detail": "夸大"}]
        # FakeLLM 每轮都会返回同样的问题;删除后复检仍会报同位置问题 -> 位置已不存在,应当阻断而非崩溃
        out, s = self.run_with([d], fact_issues=llm_err)
        self.assertIn(out.status, ("blocked", "review"))
        self.assertTrue(s.review.removed)
        self.assertEqual(s.review.removed[0]["where"], "正文第2段")

    def test_registered_caption_used_in_pipeline(self):
        d = good_draft()
        d["images"] = [{"image_id": "I1", "after_paragraph": 0, "caption": "", "reason": ""}]
        out, s = self.run_with([d])
        self.assertEqual(s.draft.images[0].caption, "主视觉")        # 来自图片索引登记


if __name__ == "__main__":
    unittest.main()
