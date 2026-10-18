import unittest

from helpers import DOC, FULL_BRIEF, FakeLLM, FakeStore, good_draft, make_prompts  # noqa: E402

from agent import compliance, evidence, export, fact_check, intake, revise, strategy  # noqa: E402
from agent.models import Brief, Draft, Evidence, Fact, ImagePlacement, Paragraph, Session  # noqa: E402
from agent.pipeline import Pipeline  # noqa: E402
from agent.prompts import PROMPTS_DIR, SPECS, PromptError, Prompts  # noqa: E402


def new_pipe(llm=None, store=None, prompts=None):
    return Pipeline(llm or FakeLLM(FULL_BRIEF), store or FakeStore(), prompts or make_prompts())


class IntakeTest(unittest.TestCase):
    def test_incomplete_asks_and_does_not_advance(self):
        llm = FakeLLM({"product": "轻薄本"})
        out = new_pipe(llm).talk(Session(), "想买轻薄本")
        self.assertEqual(out.status, "need_more_info")
        self.assertNotIn("evidence", llm.calls)          # 没问全就不碰知识库
        self.assertTrue(any("不够具体" in q for q in out.questions))

    def test_multi_turn_accumulates(self):
        llm = FakeLLM({"product": "MateBook 14", "audience": "大学新生"})
        pipe, s = new_pipe(llm), Session()
        self.assertEqual(pipe.talk(s, "推 MateBook 14,给新生").status, "need_more_info")
        llm.brief = {"scene": "开学季", "platform": "朋友圈", "goal": "种草"}
        self.assertEqual(pipe.talk(s, "开学季,朋友圈,种草").status, "review")
        self.assertEqual(s.brief.product, "MateBook 14")  # 前一轮的信息没有丢

    def test_empty_value_does_not_overwrite(self):
        b = intake.merge(Brief(product="MateBook 14"), {"product": "", "audience": "学生"})
        self.assertEqual((b.product, b.audience), ("MateBook 14", "学生"))

    def test_platform_alias_normalized(self):
        self.assertEqual(intake.normalize(Brief(platform="微信公众号")).platform, "公众号")

    def test_unsupported_platform_asks(self):
        b = intake.normalize(Brief(product="MateBook 14", audience="a", scene="b", platform="抖音", goal="种草"))
        self.assertTrue(any("暂不支持" in q for q in intake.validate(b)))

    def test_completeness_decided_by_rules_not_llm(self):
        # LLM 即使在 llm_notes 里声称信息已齐全,缺字段照样追问
        b = intake.merge(Brief(), {"product": "MateBook 14", "llm_notes": "信息已完整"})
        self.assertTrue(intake.validate(intake.normalize(b)))

    def test_unknown_goal_asks(self):
        b = intake.normalize(Brief(**{**FULL_BRIEF, "goal": "随便"}))
        self.assertTrue(any("目标" in q for q in intake.validate(b)))


class EvidenceTest(unittest.TestCase):
    def build(self, model="MateBook 14", **kw):
        return evidence.build(FakeLLM(**kw), make_prompts(), FakeStore(), model)

    def test_fabricated_fact_dropped(self):
        ev = self.build(facts=[{"text": "重量约1.36kg"}, {"text": "续航长达20小时"}])
        self.assertEqual([f.text for f in ev.facts], ["重量约1.36kg"])
        self.assertTrue(any("续航长达20小时" in d for d in ev.dropped))

    def test_every_fact_has_source(self):
        ev = self.build()
        self.assertTrue(all(f.source_path and f.source_title for f in ev.facts))
        self.assertTrue(ev.sources)

    def test_unknown_model_not_guessed(self):
        with self.assertRaises(evidence.ModelNotResolved) as ctx:
            self.build("MateBook 15")
        self.assertIn("MateBook 14", ctx.exception.candidates)   # 给出候选,不替用户选

    def test_exact_model_not_confused_with_similar(self):
        ev = self.build("MateBook 14")
        self.assertEqual({f.model for f in ev.facts}, {"MateBook 14"})
        self.assertIn("MateBook 14s", ev.candidates)

    def test_fact_mentioning_similar_model_dropped(self):
        store = FakeStore(docs={"MateBook 14": DOC + "MateBook 14s 重量约1.40kg。"})
        ev = evidence.build(FakeLLM(facts=[{"text": "MateBook 14s 重量约1.40kg"}, {"text": "内存16GB"}]), make_prompts(), store, "MateBook 14")
        self.assertEqual([f.text for f in ev.facts], ["内存16GB"])

    def test_conflict_detected_by_rule(self):
        fs = [Fact("F1", "MateBook 14", "重量约1.36kg", "t"), Fact("F2", "MateBook 14", "重量约1.40kg", "t")]
        self.assertTrue(evidence.detect_conflicts(fs))

    def test_same_value_not_conflict(self):
        fs = [Fact("F1", "MateBook 14", "重量约1.36kg", "t"), Fact("F2", "MateBook 14", "机身重量约1.36kg", "t")]
        self.assertFalse(evidence.detect_conflicts(fs))

    def test_model_matching_ignores_spacing_and_case(self):
        ev = self.build("matebook14")
        self.assertEqual(ev.facts[0].model, "MateBook 14")


class StrategyTest(unittest.TestCase):
    def test_scene_and_platform_change_strategy(self):
        a = strategy.match(Brief(scene="开学季", platform="小红书"))
        b = strategy.match(Brief(scene="职场办公", platform="小红书"))
        c = strategy.match(Brief(scene="开学季", platform="公众号"))
        self.assertNotEqual(a.scene["angle"], b.scene["angle"])
        self.assertNotEqual(a.platform["structure"], c.platform["structure"])
        self.assertNotEqual(a.platform["images"], c.platform["images"])

    def test_custom_scene_flagged(self):
        s = strategy.match(Brief(scene="直播答疑", platform="公众号"))
        self.assertFalse(s.scene["in_table"])


def ev_with(*texts):
    return Evidence(facts=[Fact(f"F{i}", "MateBook 14", t, "t", "p") for i, t in enumerate(texts, 1)],
                    images=FakeStore()._images)


B = Brief(**FULL_BRIEF)
S = strategy.match(B)


class FactCheckTest(unittest.TestCase):
    def rules(self, d, ev=None):
        return fact_check.rule_check(d, ev or ev_with("重量约1.36kg", "内存16GB"), B, S)

    def test_fabricated_number_flagged(self):
        self.assertTrue(any(i.severity == "error" for i in self.rules(Draft(body=[Paragraph("续航长达20小时", ["F1"])]))))

    def test_grounded_number_passes(self):
        self.assertFalse([i for i in self.rules(Draft(body=[Paragraph("重量约1.36kg", ["F1"])])) if i.severity == "error"])

    def test_number_from_other_fact_only_warns(self):
        issues = self.rules(Draft(body=[Paragraph("内存16GB", ["F1"])]))
        self.assertTrue(all(i.severity == "warn" for i in issues if "16gb" in i.detail.lower()))

    def test_missing_fact_id(self):
        self.assertTrue(any("不存在" in i.detail for i in self.rules(Draft(body=[Paragraph("x", ["F9"])]))))

    def test_number_without_citation(self):
        self.assertTrue(any(i.severity == "error" for i in self.rules(Draft(body=[Paragraph("重量约1.36kg", [])]))))

    def test_other_model_in_text(self):
        self.assertTrue(any(i.severity == "error" and "非主推型号" in i.detail for i in self.rules(Draft(body=[Paragraph("MateBook X Pro 更轻", [])]))))

    def test_wrong_model_fact_cited(self):
        ev = ev_with("重量约1.36kg")
        ev.facts[0].model = "MateBook X Pro"
        self.assertTrue(any("主推型号" in i.detail for i in self.rules(Draft(body=[Paragraph("轻", ["F1"])]), ev)))

    def test_image_of_other_model_flagged(self):
        d = Draft(body=[Paragraph("x")], images=[ImagePlacement("I2", 0, "图")])
        self.assertTrue(any(i.severity == "error" for i in self.rules(d)))

    def test_image_model_unconfirmed_flagged(self):
        d = Draft(body=[Paragraph("x")], images=[ImagePlacement("I3", 0, "图")])
        self.assertTrue(any("无法确认" in i.detail for i in self.rules(d)))

    def test_unknown_image_flagged(self):
        d = Draft(body=[Paragraph("x")], images=[ImagePlacement("I9", 0, "图")])
        self.assertTrue(any("不在知识库图片清单" in i.detail for i in self.rules(d)))

    def test_too_many_images(self):
        d = Draft(body=[Paragraph("x")], images=[ImagePlacement("I1", 0, "a")] * 4)   # 朋友圈上限是 3
        self.assertTrue(any("超过平台上限" in i.detail for i in self.rules(d)))

    def test_llm_issues_added_but_cannot_clear_rule_errors(self):
        llm = FakeLLM(fact_issues=[{"where": "正文第1段", "severity": "warn", "detail": "语义夸大"}])
        d = Draft(body=[Paragraph("续航长达20小时", ["F1"])])
        issues = fact_check.check(llm, make_prompts(), d, ev_with("重量约1.36kg"), B, S)
        self.assertTrue(any(i.source == "llm" for i in issues))
        self.assertTrue(any(i.source == "rule" and i.severity == "error" for i in issues))

    def test_llm_part_skipped_when_prompt_unready(self):
        d = Draft(body=[Paragraph("重量约1.36kg", ["F1"])])
        issues = fact_check.check(FakeLLM(), make_prompts(skip=("fact_review",)), d, ev_with("重量约1.36kg"), B, S)
        self.assertTrue(any("已跳过" in i.detail for i in issues))   # 明确提示,不静默
        self.assertTrue(fact_check.rule_check(d, ev_with("重量约1.36kg"), B, S) is not None)


class ComplianceTest(unittest.TestCase):
    ev = ev_with("重量约1.36kg")

    def rules(self, text):
        return compliance.rule_check(Draft(body=[Paragraph(text)]), self.ev)

    def test_absolute(self):
        self.assertTrue(self.rules("这是最好的选择"))

    def test_promise(self):
        self.assertTrue(self.rules("我们保证好用"))

    def test_price_token(self):
        self.assertTrue(any("价格" in i.detail for i in self.rules("国补后立减")))

    def test_price_number_with_yuan(self):
        self.assertTrue(any("价格" in i.detail for i in self.rules("只要5999元")))

    def test_yuan_inside_word_not_flagged(self):
        self.assertEqual(self.rules("多元化的办公方式"), [])

    def test_competitor(self):
        self.assertTrue(self.rules("性能吊打苹果"))

    def test_fake_experience(self):
        self.assertTrue(self.rules("我亲测续航很强"))

    def test_clean_passes(self):
        self.assertEqual(self.rules("适合每天通勤的同学,重量约1.36kg"), [])

    def test_word_from_knowledge_base_allowed(self):
        ev = ev_with("支持100%sRGB色域")
        self.assertEqual(compliance.rule_check(Draft(body=[Paragraph("支持100%sRGB色域")]), ev), [])

    def test_llm_only_adds(self):
        llm = FakeLLM(comp_issues=[{"where": "标题", "severity": "error", "detail": "暗示性对比"}])
        issues = compliance.check(llm, make_prompts(), Draft(title="好"), self.ev)
        self.assertTrue(any(i.source == "llm" and i.severity == "error" for i in issues))


class PipelineTest(unittest.TestCase):
    def test_happy_path(self):
        pipe, s = new_pipe(), Session()
        out = pipe.talk(s, "go")
        self.assertEqual(out.status, "review", out.reason)
        self.assertTrue(s.review.passed)
        self.assertEqual([f.id for f in s.evidence.facts], ["F1", "F2"])

    def test_writer_system_only_for_writing(self):
        llm = FakeLLM(FULL_BRIEF)
        new_pipe(llm).talk(Session(), "go")
        for stage in ("intake", "evidence", "fact_review", "compliance_review"):
            self.assertIsNone(llm.systems[stage], stage)
        self.assertEqual(llm.systems["compose"], make_prompts().writer_system)

    def test_unknown_model_asks_to_choose(self):
        llm = FakeLLM({**FULL_BRIEF, "product": "MateBook 15"})
        out = new_pipe(llm).talk(Session(), "go")
        self.assertEqual(out.status, "need_choice")
        self.assertIn("MateBook 14", out.candidates)
        self.assertNotIn("compose", llm.calls)

    def test_conflict_blocks(self):
        store = FakeStore(docs={"MateBook 14": "重量约1.36kg。重量约1.40kg。"})
        llm = FakeLLM(FULL_BRIEF, facts=[{"text": "重量约1.36kg"}, {"text": "重量约1.40kg"}])
        out = new_pipe(llm, store).talk(Session(), "go")
        self.assertEqual(out.status, "blocked")
        self.assertNotIn("compose", llm.calls)

    def test_rewrite_fixes_bad_draft(self):
        bad = good_draft()
        bad["body"] = [{"text": "续航长达20小时,国补立减。", "fact_ids": ["F1"]}]
        llm = FakeLLM(FULL_BRIEF, drafts=[bad, good_draft()])
        s = Session()
        out = new_pipe(llm).talk(s, "go")
        self.assertEqual(out.status, "review")
        self.assertIn("rewrite", llm.calls)
        self.assertNotIn("国补", s.draft.body[0].text)

    def test_unfixable_draft_blocked(self):
        bad = good_draft()
        bad["body"] = [{"text": "续航长达20小时", "fact_ids": ["F1"]}]
        out = new_pipe(FakeLLM(FULL_BRIEF, drafts=[bad])).talk(Session(), "go")
        self.assertEqual(out.status, "blocked")

    def test_export_four_sections_and_sources(self):
        s = Session()
        new_pipe().talk(s, "go")
        md = export.to_markdown(s)
        for h in ("# 一、场景判断", "# 二、知识库依据", "# 三、图文软文", "# 四、核验结果"):
            self.assertIn(h, md)
        self.assertIn("产品参数/MateBook 14/article.md", md)       # 来源标注
        self.assertNotIn("F1]", md.split("# 三")[1].split("# 四")[0])  # 事实编号不进软文


class FeedbackTest(unittest.TestCase):
    def ready(self, **llm_kw):
        llm = FakeLLM(FULL_BRIEF, **llm_kw)
        pipe, s = new_pipe(llm), Session()
        self.assertEqual(pipe.talk(s, "go").status, "review")
        return pipe, s, llm

    def count(self, llm, stage):
        return llm.calls.count(stage)

    def test_confirm(self):
        pipe, s, _ = self.ready()
        self.assertEqual(pipe.feedback(s, "可以").status, "done")

    def test_rewrite_reuses_evidence_and_strategy(self):
        pipe, s, llm = self.ready(intent={"action": "rewrite", "note": "语气再轻松一点"})
        ev, st = s.evidence, s.strategy
        out = pipe.feedback(s, "文案不行")
        self.assertEqual(out.status, "review")
        self.assertIs(s.evidence, ev)
        self.assertIs(s.strategy, st)
        self.assertEqual(self.count(llm, "evidence"), 1)           # 资料没有重新抽取
        self.assertEqual(self.count(llm, "compose"), 2)
        self.assertIn("语气再轻松一点", s.brief.extra)

    def test_change_platform_rematches_strategy_only(self):
        pipe, s, llm = self.ready(intent={"action": "change_platform", "value": "小红书"})
        ev = s.evidence
        pipe.feedback(s, "换成小红书")
        self.assertEqual(s.brief.platform, "小红书")
        self.assertEqual(s.strategy.platform["name"], "小红书")
        self.assertIs(s.evidence, ev)
        self.assertEqual(self.count(llm, "evidence"), 1)
        self.assertEqual(self.count(llm, "intake"), 1)             # 没有重走结构化

    def test_change_scene_rematches_strategy(self):
        pipe, s, llm = self.ready(intent={"action": "change_scene", "value": "职场办公"})
        pipe.feedback(s, "改成职场")
        self.assertEqual(s.strategy.scene["name"], "职场办公")
        self.assertEqual(self.count(llm, "evidence"), 1)

    def test_change_product_restarts_from_intake(self):
        pipe, s, llm = self.ready(intent={"action": "change_product", "value": "MateBook 14s"})
        llm.brief = {}                                              # 重新抽取时没有新信息,沿用已有字段
        s.history.append({"role": "用户", "content": "改推 MateBook 14s"})
        out = pipe.feedback(s, "改推 MateBook 14s")
        self.assertEqual(self.count(llm, "intake"), 2)             # 结构化重来了一遍
        self.assertEqual(s.brief.product, "MateBook 14s")
        # 假知识库里 14s 有型号但没有资料:没有资料就不调用模型摘录,直接停止
        self.assertEqual(self.count(llm, "evidence"), 1)
        self.assertEqual(out.status, "blocked")
        self.assertIsNone(s.draft)                                 # 旧稿已作废,不会残留到新产品上
        self.assertIsNone(s.strategy)

    def test_unclear_asks_back(self):
        pipe, s, llm = self.ready(intent={"action": "unclear"})
        out = pipe.feedback(s, "嗯……")
        self.assertEqual(out.status, "need_more_info")
        self.assertEqual(self.count(llm, "compose"), 1)            # 不乱改

    def test_unsupported_platform_asks_back(self):
        pipe, s, _ = self.ready(intent={"action": "change_platform", "value": "抖音"})
        self.assertEqual(pipe.feedback(s, "换抖音").status, "need_more_info")
        self.assertEqual(s.brief.platform, "朋友圈")                # 需求没有被改坏

    def test_unknown_action_treated_as_unclear(self):
        pipe, s, _ = self.ready(intent={"action": "delete_everything"})
        self.assertEqual(pipe.feedback(s, "x").status, "need_more_info")


class PromptsTest(unittest.TestCase):
    def full(self):
        return {n: "任务 " + " ".join("{{%s}}" % v for v in req) for n, req in SPECS.items()}

    def test_repo_skeleton_reported_unfinished(self):
        p = Prompts.load(strict=False)
        # 仓库里的提示词都还是骨架,每个环节都应被识别为未完成
        self.assertEqual(set(p.problems), set(SPECS))

    def test_strict_load_rejects_skeleton(self):
        with self.assertRaises(PromptError):
            Prompts.load()

    def test_lenient_load_fails_only_when_stage_used(self):
        p = Prompts.load(strict=False)
        with self.assertRaises(PromptError):
            p.render("intake", {"已有信息": "", "对话记录": "", "场景选项": "", "平台选项": ""})

    def test_missing_and_unknown_variable(self):
        t = self.full(); t["evidence"] = "无变量"
        self.assertIn("缺少变量", Prompts(t, strict=False).problems["evidence"][0])
        t = self.full(); t["evidence"] += " {{拼错}}"
        self.assertTrue(any("未定义" in m for m in Prompts(t, strict=False).problems["evidence"]))

    def test_comments_stripped(self):
        t = self.full(); t["writer_system"] = "<!-- 给编写者看的 -->\n你是Agent"
        self.assertEqual(Prompts(t).writer_system, "你是Agent")

    def test_render_does_not_expand_values(self):
        out = Prompts(self.full()).render("evidence", {"型号": "{{资料标题}}", "资料标题": "T", "资料全文": "x"})
        self.assertIn("{{资料标题}}", out)

    def test_repo_prompt_files_match_specs(self):
        for name in SPECS:
            self.assertTrue((PROMPTS_DIR / f"{name}.md").is_file(), name)


class ReviseApplyTest(unittest.TestCase):
    def test_apply_table(self):
        b = Brief(**FULL_BRIEF)
        self.assertEqual(revise.apply(b, {"action": "confirm", "value": "", "note": ""}, "")[1], "done")
        self.assertEqual(revise.apply(b, {"action": "rewrite", "value": "", "note": "短一点"}, "")[1], "generate")
        nb, step = revise.apply(b, {"action": "change_product", "value": "MateBook X Pro", "note": ""}, "")
        self.assertEqual((nb.product, nb.extra, step), ("MateBook X Pro", "", "intake"))
        self.assertEqual(b.product, "MateBook 14")                 # 原对象不被修改


if __name__ == "__main__":
    unittest.main()
