"""小节篇幅：平台字数目标、每块段落数、事实上限与篇幅提示。"""
import unittest

import helpers  # noqa: F401  把项目根目录加入测试路径

from agent import section
from agent.models import ContentBlock, ContentPlan, Draft, Paragraph


def platform(name, length):
    return {"name": name, "length": length}


class LengthTargetTest(unittest.TestCase):
    def test_wechat_body_target_read_from_policy(self):
        self.assertEqual(section.body_chars(platform("公众号", "正文800-1500字,分3-5个小节")), (800, 1500))

    def test_missing_length_falls_back_to_platform_default(self):
        self.assertEqual(section.body_chars(platform("小红书", "")), (420, 420))

    def test_single_number_is_used_as_both_bounds(self):
        self.assertEqual(section.body_chars(platform("门店海报", "全部文字不超过80字")), (80, 80))

    def test_wechat_blocks_are_split_by_section_count(self):
        p = platform("公众号", "正文800-1500字,分3-5个小节")
        # 5 个小节分摊 1500 字,每块约 300 字、两段
        self.assertEqual(section.expected_chars(p, "body"), 300)
        self.assertEqual(section.paragraphs_per_block(p), 2)

    def test_short_copy_stays_one_paragraph(self):
        p = platform("门店海报", "全部文字不超过80字")
        self.assertEqual(section.paragraphs_per_block(p), 1)

    def test_fact_cap_grows_with_length(self):
        self.assertEqual(section.fact_cap(platform("朋友圈", "全文80-150字"), "body"), 2)
        self.assertEqual(section.fact_cap(platform("公众号", "正文800-1500字"), "body"), 6)
        self.assertEqual(section.fact_cap(platform("公众号", "正文800-1500字"), "opening"), 0)


class ShortDraftTest(unittest.TestCase):
    p = platform("公众号", "正文800-1500字,分3-5个小节")

    def test_one_short_sentence_is_flagged(self):
        self.assertTrue(section.is_short(self.p, "body", ["内存16GB。"]))

    def test_developed_section_is_not_flagged(self):
        text = "这款机型采用16GB内存，" + "日常多开文档与浏览器时，切换应用更从容。" * 12
        self.assertFalse(section.is_short(self.p, "body", [text]))

    def test_terse_but_informative_copy_is_not_forced_longer(self):
        # 300 字一节、只写六十余字虽偏短，但已传达完整信息，不再退回
        text = "内存16GB，屏幕14.2英寸，重量约1.36kg。" * 3
        self.assertFalse(section.is_short(self.p, "body", [text]))

    def test_only_body_blocks_are_measured(self):
        self.assertFalse(section.is_short(self.p, "opening", ["短标题"]))
        self.assertEqual(section.expected_chars(self.p, "closing"), 0)

    def test_hint_asks_to_expand_using_whitelisted_facts(self):
        hint = section.short_hint(self.p, "body", ["内存16GB。"])
        self.assertIn("篇幅", hint)
        self.assertIn("事实白名单", hint)


class PlanningTargetTest(unittest.TestCase):
    def test_body_blocks_are_counted_for_sharing_length(self):
        plan = ContentPlan([
            ContentBlock("B1", "opening", "标题和导语"),
            ContentBlock("B2", "body", "小节一"),
            ContentBlock("B3", "body", "小节二"),
            ContentBlock("B4", "closing", "收束"),
        ])
        self.assertEqual(section.body_blocks(plan), 2)
        self.assertEqual(section.expected_chars(platform("公众号", "正文800-1000字"), "body", 2), 500)


if __name__ == "__main__":
    unittest.main()