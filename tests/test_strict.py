import unittest

from helpers import FULL_BRIEF, FakeLLM, good_draft, make_prompts  # noqa: F401

from agent import fact_check, norm, strategy  # noqa: E402
from agent.models import Brief, Draft, Evidence, Fact, Paragraph, Session  # noqa: E402
from agent.pipeline import Pipeline  # noqa: E402
from helpers import FakeStore  # noqa: E402

B = Brief(**FULL_BRIEF)
S = strategy.match(B)
PORTS = "USB-C 接口 × 1(全功能) USB3.2 Gen1 × 2 HDMI × 1 蓝牙 5.1"


def ev(*texts):
    return Evidence(facts=[Fact(f"F{i}", "MateBook 14", t, "t", "p") for i, t in enumerate(texts, 1)])


def check(text, *facts):
    d = Draft(body=[Paragraph(text, ["F1"])])
    return [i for i in fact_check.rule_check(d, ev(*(facts or (PORTS,))), B, S) if "规格版本" in i.detail]


class SpecVersionTest(unittest.TestCase):
    def test_invented_hdmi_version_blocked(self):
        issues = check("配有1个HDMI 2.1接口")
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].severity, "error")

    def test_invented_bluetooth_version_blocked(self):
        self.assertTrue(check("支持蓝牙5.3"))

    def test_invented_usb_version_blocked(self):
        self.assertTrue(check("2个USB-A 3.0接口"))

    def test_version_without_basis_in_any_fact(self):
        self.assertTrue(check("支持Wi-Fi 6"))

    def test_same_version_different_spelling_passes(self):
        self.assertFalse(check("2个USB-A 3.2 Gen1接口"))        # 资料写 USB3.2 Gen1
        self.assertFalse(check("两个 USB 3.2 Gen1 接口"))
        self.assertFalse(check("蓝牙 5.1"))

    def test_no_version_claim_passes(self):
        self.assertFalse(check("1个HDMI接口和全功能USB-C"))

    def test_checked_in_title_and_cta_too(self):
        d = Draft(title="HDMI 2.1 直连大屏", body=[Paragraph("x", ["F1"])])
        issues = fact_check.rule_check(d, ev(PORTS), B, S)
        self.assertTrue(any(i.where == "标题" and "规格版本" in i.detail for i in issues))

    def test_spec_versions_normalization(self):
        sv = norm.spec_versions
        self.assertEqual(sv("USB-A 3.2 Gen1"), sv("USB3.2 Gen1"))
        self.assertEqual(sv("Wi-Fi 6"), sv("WiFi6"))
        self.assertNotEqual(sv("USB 3.2"), sv("USB 3.0"))
        self.assertEqual(sv("没有版本的描述"), set())


class LLMSeverityTest(unittest.TestCase):
    def test_llm_warn_is_treated_as_error(self):
        issues = norm.llm_issues({"issues": [{"where": "正文第2段", "severity": "warn", "detail": "把HDMI写成2.1"}]}, "fact")
        self.assertEqual(issues[0].severity, "error")
        self.assertEqual(issues[0].source, "llm")

    def test_llm_missing_severity_is_error(self):
        self.assertEqual(norm.llm_issues({"issues": [{"where": "标题", "detail": "夸大"}]}, "fact")[0].severity, "error")

    def test_where_normalized_and_empty_detail_ignored(self):
        out = norm.llm_issues({"issues": [{"where": "body[0].text", "detail": "x"}, {"where": "标题", "detail": ""}, "junk"]}, "fact")
        self.assertEqual([(i.where, i.detail) for i in out], [("正文第1段", "x")])

    def test_unrecognizable_where_kept_for_blocking(self):
        self.assertEqual(norm.llm_issues({"issues": [{"where": "整体语气", "detail": "夸张"}]}, "compliance")[0].where, "整体语气")


class PipelineStrictTest(unittest.TestCase):
    def run_with(self, drafts, **kw):
        s = Session()
        out = Pipeline(FakeLLM(FULL_BRIEF, drafts=drafts, **kw), FakeStore(), make_prompts()).talk(s, "go")
        return out, s

    def draft_with(self, *paras):
        d = good_draft()
        d["body"] = [{"text": t, "fact_ids": ["F1"]} for t in paras]
        return d

    def test_llm_warning_no_longer_lets_bad_paragraph_through(self):
        # LLM 把问题标成 warn;以前会放行,现在按 error 处理并自动删除
        d = self.draft_with("重量约1.36kg,背着不累。", "内存16GB。")
        warn = [{"where": "正文第2段", "severity": "warn", "detail": "夸大"}]
        out, s = self.run_with([d], comp_issues=warn)
        self.assertEqual([p.text for p in s.draft.body][:1], ["重量约1.36kg,背着不累。"])
        self.assertEqual(s.review.removed[0]["where"], "正文第2段")

    def test_invented_version_removed_with_trace(self):
        d = self.draft_with("重量约1.36kg。", "配有1个HDMI 2.1接口。")
        store = FakeStore(docs={"MateBook 14": "MateBook 14 重量约1.36kg。内存16GB。屏幕14.2英寸。HDMI × 1"})
        s = Session()
        out = Pipeline(FakeLLM(FULL_BRIEF, drafts=[d], facts=[{"text": "重量约1.36kg"}, {"text": "HDMI × 1"}]), store, make_prompts()).talk(s, "go")
        self.assertEqual(out.status, "review", out.reason)
        self.assertEqual([p.text for p in s.draft.body], ["重量约1.36kg。"])
        self.assertIn("规格版本", s.review.removed[0]["reason"])


if __name__ == "__main__":
    unittest.main()
