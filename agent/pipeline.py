"""核心调度器。

  多轮澄清 → 规范任务说明 → 资料与策略 → 文章规划
  → 生成/重写与审核双队列 → 事实锁定连贯性编辑 → 全文终审 → 确认/修改

每个方法返回 Outcome,status 为:
  need_more_info  需要用户补充(questions)
  need_choice     型号无法确认,请用户从候选里选(questions + candidates)
  blocked         资料冲突或多轮改写后仍不达标,停止输出(reason)
  review          稿件已生成并通过核验,等待用户确认(阶段7)
  done            用户已确认
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import briefing, chunk_pipeline, chunk_review, compliance, compose, evidence, export, fact_check, intake, planning, prune, revise, strategy
from .knowledge import KnowledgeError
from .llm import LLMError
from .models import Brief, ContextPackage, Draft, Review, Session
from .norm import norm_model
from .prompts import PromptError, Prompts

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"
ProgressCallback = Callable[[str, str], None]


@dataclass
class Outcome:
    status: str
    session: Session
    questions: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    reason: str = ""
    refusals: list[str] = field(default_factory=list)


class Pipeline:
    def __init__(
        self,
        llm,
        store,
        prompts: Prompts,
        review_mode: str = "strict",
        performance_provider: Callable[[Brief], dict] | None = None,
        preference_provider: Callable[[], dict] | None = None,
    ):
        if review_mode not in ("strict", "relaxed"):
            raise ValueError("review_mode 必须为 strict 或 relaxed")
        self.llm = llm
        self.store = store
        self.prompts = prompts
        self.review_mode = "strict" if getattr(store, "requires_strict_review", False) else review_mode
        self.performance_provider = performance_provider
        self.preference_provider = preference_provider

    def _llm(self, stage: str):
        resolve = getattr(self.llm, "for_stage", None)
        return resolve(stage) if resolve else self.llm

    # ---- 记录 ----
    @staticmethod
    def _trace(s: Session, stage: str, **info) -> None:
        s.trace.append({"stage": stage, "at": time.strftime("%H:%M:%S"), **info})

    @staticmethod
    def _progress(callback: ProgressCallback | None, title: str, detail: str) -> None:
        """向界面报告可核验的阶段摘要，不暴露模型内部推理。"""
        if callback:
            callback(title, detail)

    def _match_strategy(self, s: Session) -> None:
        performance = {}
        if self.performance_provider:
            try:
                performance = self.performance_provider(s.brief) or {}
            except Exception as exc:  # 宣传数据只是参考，读取失败不能阻断主创作链路。
                self._trace(s, "宣传数据参考", result="读取失败，已跳过", error=str(exc))
        s.strategy = strategy.match(s.brief, performance)
        s.context_package = None
        refs = performance.get("references", []) if performance else []
        self._trace(
            s, "3 策略匹配", scene=s.strategy.scene.get("name"),
            platform=s.strategy.platform["name"], performance_references=len(refs),
            top_reference_weight=(refs[0].get("reference_weight") if refs else None),
        )

    def _assemble_context(self, s: Session) -> None:
        """把所有下游输入收拢为带权限边界的统一上下文包。"""
        if not s.evidence or not s.strategy:
            s.context_package = None
            return
        preferences = {}
        if self.preference_provider:
            try:
                preferences = self.preference_provider() or {}
            except Exception as exc:  # 长期偏好是表达参考，读取失败不能升级为主链路故障。
                self._trace(s, "用户偏好", result="读取失败，已跳过", error=str(exc))
        allowed_preferences = {
            key: value for key, value in preferences.items()
            if key in {
                "preferred_tone", "preferred_platforms", "audience_focus",
                "content_preferences", "avoid_topics", "notes", "updated_at",
            } and value not in (None, "")
        }
        manifest = []
        hashes = s.evidence.review_stats.get("source_hashes", {})
        for path in dict.fromkeys(s.evidence.sources + [
            row.get("path", "") for row in s.evidence.context if row.get("path")
        ]):
            manifest.append({"path": path, "sha256": hashes.get(path, "")})
        s.context_package = ContextPackage(
            canonical_brief=s.canonical_brief,
            facts=list(s.evidence.facts),
            images=list(s.evidence.images),
            expression_references=list(s.evidence.context),
            user_preferences=allowed_preferences,
            performance_references=list(s.strategy.performance.get("references", [])),
            scene=dict(s.strategy.scene),
            platform=dict(s.strategy.platform),
            source_manifest=manifest,
            verification={},
        )
        self._trace(
            s, "3.5 统一上下文装配",
            contract=s.context_package.contract_version,
            facts=len(s.context_package.facts), images=len(s.context_package.images),
            expression_references=len(s.context_package.expression_references),
            performance_references=len(s.context_package.performance_references),
            preferences=sorted(s.context_package.user_preferences),
        )

    def _normalize_brief(self, s: Session) -> None:
        value = briefing.canonicalize(s.brief)
        if value != s.canonical_brief:
            s.canonical_brief = value
            self._trace(s, "1.5 规范任务说明", canonical_brief=value)

    # ---- 阶段1:多轮追问 ----
    def talk(self, s: Session, user_text: str, progress: ProgressCallback | None = None) -> Outcome:
        """处理一轮用户输入。信息不完整返回 need_more_info;完整后自动进入后续阶段。"""
        s.history.append({"role": "用户", "content": user_text})
        self._progress(progress, "正在理解需求", "识别产品、受众、场景、平台和内容目标")
        # 用户直接回复库内型号时，这是明确选择，不能再被旧需求覆盖。
        selected = None
        if intake.MODEL_HINT.search(user_text):
            try:
                selected = next((name for name in self.store.models()
                                 if norm_model(name) == norm_model(user_text.strip())), None)
            except (KnowledgeError, OSError) as exc:
                return self._block(s, f"本地知识库读取失败：{exc}")
        if selected:
            s.brief.product = selected
            s.brief.product_type = ""
            s.evidence = s.strategy = s.context_package = s.content_plan = s.draft = s.review = None
            s.canonical_brief = ""
            s.brief = intake.normalize(s.brief)
            questions = intake.validate(s.brief)
        else:
            s.brief, questions = intake.next_step(self._llm("intake"), self.prompts, s.brief, s.history)
        self._trace(s, "1 结构化需求", brief=s.brief.__dict__, questions=questions)
        if questions:
            self._progress(progress, "需求需要补充", f"还有 {len(questions)} 项关键信息需要确认")
            s.history.append({"role": "助手", "content": "\n".join(questions)})
            return Outcome("need_more_info", s, questions=questions)
        self._progress(
            progress, "需求已确认",
            f"{s.brief.product} · {s.brief.audience} · {s.brief.scene} · {s.brief.platform}",
        )
        self._normalize_brief(s)
        return self.produce(s, progress=progress)

    # ---- 阶段2-6:从需求到通过核验的稿件 ----
    def produce(
        self, s: Session, reuse_evidence: bool = False, progress: ProgressCallback | None = None
    ) -> Outcome:
        b = s.brief
        self._normalize_brief(s)
        if not (reuse_evidence and s.evidence):
            out = self._match_evidence(s, progress)
            if out:
                return out
        self._match_strategy(s)                                                # 阶段3
        refs = s.strategy.performance.get("references", [])
        reference_note = f"，并参考 {len(refs)} 篇历史高表现作品" if refs else ""
        self._progress(
            progress, "策略已匹配",
            f"按{s.strategy.scene.get('name')}场景和{s.strategy.platform['name']}规则组织内容{reference_note}",
        )
        return self.generate(s, progress=progress)

    def _match_evidence(self, s: Session, progress: ProgressCallback | None = None) -> Outcome | None:
        b = s.brief
        self._progress(progress, "正在匹配知识库", f"只检索与“{b.product}”明确对应的资料和图片")
        try:
            if not self.store.connect()["connected"]:
                return self._block(s, f"本地知识库“{self.store.kb_name}”不可用，请检查 KB_LOCAL_DIR 和产品事实目录")
            collect = getattr(self.store, "collect", None)
            s.evidence = (collect(b, progress)
                          if collect else evidence.build(self._llm("evidence"), self.prompts, self.store, b.product))
        except (KnowledgeError, OSError) as exc:
            return self._block(s, f"本地知识库读取失败：{exc}")
        except evidence.ModelNotResolved as e:
            self._trace(s, "2 资料匹配", result="型号未确认", candidates=e.candidates)
            hint = f"知识库里没有型号“{e.model}”。" + (f"你是想推广其中之一吗?{'、'.join(e.candidates)}" if e.candidates else "请确认型号。")
            s.history.append({"role": "助手", "content": hint})
            return Outcome("need_choice", s, questions=[hint], candidates=e.candidates)
        ev = s.evidence
        self._trace(
            s, "2 资料匹配", facts=len(ev.facts), images=len(ev.images), sources=ev.sources,
            dropped=ev.dropped, conflicts=ev.conflicts, similar_models=ev.candidates,
            usage_review=ev.review_stats,
        )
        self._progress(
            progress, "资料匹配完成",
            f"找到 {len(ev.facts)} 条可用事实、{len([i for i in ev.images if i.model])} 张已确认图片",
        )
        if ev.conflicts:
            return self._block(s, "知识库资料存在冲突,请人工确认后再生成:" + ";".join(ev.conflicts))
        if not ev.facts:
            return self._block(s, "未能从知识库资料中提取到可用的事实点,无法生成有依据的内容")
        return None

    # ---- 分块生产 → 连贯性编辑 → 全文终审 ----
    def _review(self, s: Session, draft: Draft) -> Review:
        issues = fact_check.check(self._llm("fact_review"), self.prompts, draft, s.evidence, s.brief, s.strategy)
        issues += compliance.check(self._llm("compliance_review"), self.prompts, draft, s.evidence)
        if self.review_mode == "relaxed":
            for issue in issues:
                if issue.source == "llm":
                    issue.severity = "warn"
        return Review(issues)

    @staticmethod
    def _reset_target_blocks(s: Session, target: str) -> list[str]:
        """只使目标块及依赖它的下游块失效；返回被重置的块 ID。"""
        if not s.content_plan:
            return []
        blocks = s.content_plan.blocks
        text = (target or "").strip().lower()
        selected = {block.id for block in blocks if block.id.lower() == text}
        if not selected and any(word in text for word in ("opening", "标题", "导语", "开篇")):
            selected = {block.id for block in blocks if block.kind == "opening"}
        if not selected and any(word in text for word in ("closing", "行动引导", "cta", "标签", "结尾", "收束")):
            selected = {block.id for block in blocks if block.kind == "closing"}
        paragraph = re.search(r"(?:正文)?第\s*(\d+)\s*段", text)
        if not selected and paragraph:
            wanted = int(paragraph.group(1))
            offset = 0
            for block in blocks:
                count = len(block.draft.body) if block.kind == "body" and block.draft else 0
                if offset < wanted <= offset + count:
                    selected.add(block.id)
                    break
                offset += count
        if not selected and any(word in text for word in ("body", "正文", "卖点")):
            selected = {block.id for block in blocks if block.kind == "body"}
        if not selected:
            return []

        changed = True
        while changed:
            changed = False
            for block in blocks:
                if block.id not in selected and any(dep in selected for dep in block.depends_on):
                    selected.add(block.id)
                    changed = True
        for block in blocks:
            if block.id in selected:
                block.status = "pending_generate"
                block.attempts = 0
                block.draft = None
                block.issues = []
                block.removed = []
        return [block.id for block in blocks if block.id in selected]

    def generate(
        self, s: Session, progress: ProgressCallback | None = None, reuse_plan: bool = False
    ) -> Outcome:
        valid = getattr(self.store, "still_valid", None)
        if valid and s.evidence and not valid(s.evidence):
            out = self._match_evidence(s, progress)
            if out:
                return out
        self._assemble_context(s)
        if not (reuse_plan and s.content_plan):
            s.content_plan = None
            self._progress(progress, "正在规划文章", "确定内容块、事实白名单和必要依赖")
            try:
                s.content_plan = planning.create(
                    self._llm("content_plan"), self.prompts, s.canonical_brief,
                    s.evidence, s.strategy, s.context_package,
                )
            except (LLMError, PromptError) as exc:
                return self._block(s, f"文章规划模型调用失败：{exc}")
            self._trace(s, "4 文章分块规划", blocks=[{
                "id": block.id, "kind": block.kind, "objective": block.objective,
                "fact_ids": block.fact_ids, "depends_on": block.depends_on,
            } for block in s.content_plan.blocks])
            self._progress(progress, "文章规划完成", f"共 {len(s.content_plan.blocks)} 个内容块，启动生成与审核双队列")
        else:
            pending = [block.id for block in s.content_plan.blocks if block.status != "passed"]
            self._progress(progress, "复用文章规划", f"仅重新处理受影响块：{'、'.join(pending)}")
        self._progress(progress, "正在核验内容", "审核模型将逐块检查，问题块会返回生成/重写队列")

        flow = chunk_pipeline.run(
            self._llm("chunk_generate"), self._llm("chunk_review"), self.prompts,
            s.canonical_brief, s.brief, s.evidence, s.strategy, s.content_plan,
            self.review_mode, progress, s.context_package,
        )
        self._trace(
            s, "5 分块队列", events=flow.events, metrics=flow.metrics,
            result="blocked" if flow.error else "passed",
        )
        if flow.error or flow.draft is None:
            s.draft = flow.draft
            s.review = None
            return Outcome("blocked", s, reason=flow.error or "分块流水线未能形成完整稿件", refusals=flow.refusals)

        assembled = flow.draft
        if not assembled.body:
            s.draft = assembled
            return Outcome("blocked", s, reason="所有内容块合并后正文为空，已停止输出", refusals=flow.refusals)

        self._progress(progress, "正在进行整文连贯性编辑", "只调整衔接、指代、语气和重复，不改变事实")
        try:
            draft, coherence_violations, coherence_refusals = compose.edit_coherence(
                self._llm("coherence"), self.prompts, s.canonical_brief, assembled, s.evidence
            )
        except (LLMError, PromptError) as exc:
            draft, coherence_violations, coherence_refusals = (
                assembled, [f"连贯性编辑调用失败，已保留逐块审核后的原始合并稿：{exc}"], []
            )
        coherence_notes = list(coherence_violations) + list(coherence_refusals)
        if draft is not assembled:
            try:
                semantic_changes = chunk_review.check_coherence(
                    self._llm("coherence_review"), self.prompts, assembled, draft, s.evidence
                )
            except (LLMError, PromptError) as exc:
                semantic_changes = [f"连贯性事实锁审核调用失败，已回退原始合并稿：{exc}"]
            if semantic_changes:
                coherence_notes.extend(semantic_changes)
                draft = assembled
        self._trace(s, "6 整文连贯性编辑", applied=draft is not assembled, notes=coherence_notes)

        self._progress(progress, "正在进行全文终审", "检查合并后的事实、图片归属和营销合规")
        try:
            review = self._review(s, draft)
        except (LLMError, PromptError) as exc:
            s.draft, s.review = draft, None
            self._trace(s, "7 全文终审", result="调用失败", error=str(exc))
            return Outcome("blocked", s, reason=f"全文终审模型调用失败，稿件未放行：{exc}",
                           refusals=flow.refusals)
        # 连贯性编辑即使通过结构锁，也可能产生语义事实问题；此时先回退到全部已审核块的原始合并稿。
        if not review.passed and draft is not assembled:
            try:
                original_review = self._review(s, assembled)
            except (LLMError, PromptError) as exc:
                s.draft, s.review = assembled, None
                self._trace(s, "7 全文终审", result="回退稿审核调用失败", error=str(exc))
                return Outcome(
                    "blocked", s,
                    reason=f"连贯性编辑未通过，原始合并稿终审调用失败，稿件未放行：{exc}",
                    refusals=flow.refusals,
                )
            if original_review.passed:
                coherence_notes.append("连贯性编辑未通过全文终审，已回退到原始合并稿")
                draft, review = assembled, original_review
        removed = list(flow.removed)
        if not review.passed:
            max_images = s.strategy.platform.get("images", {}).get("max", 99)
            pruned_draft, final_removed, unresolved = prune.apply(draft, review.errors, max_images)
            if final_removed and not unresolved:
                draft = pruned_draft
                removed.extend(final_removed)
                try:
                    review = self._review(s, draft)
                except (LLMError, PromptError) as exc:
                    s.draft, s.review = draft, None
                    self._trace(s, "7 全文终审", result="安全删除后复审调用失败", error=str(exc))
                    return Outcome(
                        "blocked", s,
                        reason=f"安全删除后的全文终审调用失败，稿件未放行：{exc}",
                        refusals=flow.refusals,
                    )
                coherence_notes.append("全文终审定位到的问题已安全删除并重新审核")
        review.removed = removed
        self._trace(s, "7 全文终审", errors=len(review.errors),
                    warns=len(review.issues) - len(review.errors), coherence_notes=coherence_notes)
        s.draft, s.review = draft, review

        if not draft.body:
            return Outcome("blocked", s, reason="全文终审删除问题内容后正文已为空，已停止输出", refusals=flow.refusals)
        if not review.passed:
            return Outcome("blocked", s, reason="全文终审仍有无法安全处理的问题，已停止输出", refusals=flow.refusals)
        warn_count = len(review.issues) - len(review.errors)
        self._progress(progress, "生成完成", f"阻断项已通过；{warn_count} 项提示可在右侧核验卡查看")
        return Outcome("review", s, refusals=flow.refusals)

    def resume(self, s: Session, progress: ProgressCallback | None = None) -> Outcome:
        """从持久化检查点继续；仅重跑未冻结阶段和中断中的内容块。"""
        s.prepare_resume()
        if s.draft and s.review and s.review.passed:
            self._progress(progress, "检查点已恢复", "已恢复通过核验的稿件，可继续修改或确认")
            return Outcome("review", s)
        questions = intake.validate(s.brief)
        if questions:
            self._progress(progress, "检查点已恢复", f"还有 {len(questions)} 项关键信息需要确认")
            return Outcome("need_more_info", s, questions=questions)
        self._progress(progress, "检查点已恢复", "从最近安全阶段继续处理")
        self._normalize_brief(s)
        if s.content_plan and s.evidence and s.strategy:
            return self.generate(s, progress=progress, reuse_plan=True)
        if s.evidence:
            return self.produce(s, reuse_evidence=True, progress=progress)
        return self.produce(s, progress=progress)

    # ---- 阶段7:确认与修改 ----
    def feedback(
        self,
        s: Session,
        user_text: str,
        progress: ProgressCallback | None = None,
        intent_override: dict | None = None,
    ) -> Outcome:
        s.history.append({"role": "用户", "content": user_text})
        self._progress(progress, "正在理解修改要求", "判断需要调整文案、平台、场景还是产品")
        # 下拉框等结构化界面动作直接给出可靠意图，避免再让模型猜测“本次创作设置”。
        intent = intent_override or revise.classify(
            self._llm("revise_intent"), self.prompts, s.brief, s.draft, user_text
        )
        self._trace(s, "7 修改意图", **intent)
        new_brief, step = revise.apply(s.brief, intent, user_text)
        if new_brief is None:
            q = "没太明白你想改什么。是想换场景、换平台、换产品,还是只是调整文案?"
            s.history.append({"role": "助手", "content": q})
            return Outcome("need_more_info", s, questions=[q])
        s.brief = new_brief
        self._normalize_brief(s)
        if step == "done":
            self._progress(progress, "定稿完成", "当前版本已确认，可下载发布成品或完整报告")
            return Outcome("done", s)
        if step == "generate":                  # 只重写文案:需求、资料、策略都复用
            self._progress(progress, "修改范围已确认", "复用已核验资料，只重新生成文案")
            s.content_plan = None
            return self.generate(s, progress=progress)
        if step == "block":
            targets = self._reset_target_blocks(s, intent.get("value", ""))
            if not targets:
                q = "没能定位要修改的内容块。请说明要改标题、导语、正文第几段、行动引导，或要求整篇重写。"
                s.history.append({"role": "助手", "content": q})
                return Outcome("need_more_info", s, questions=[q])
            self._trace(s, "局部回退", target=intent.get("value", ""), invalidated=targets)
            self._progress(progress, "修改范围已确认", f"仅重做 {'、'.join(targets)}，其余已通过块继续复用")
            return self.generate(s, progress=progress, reuse_plan=True)
        if step == "strategy":                  # 换场景/平台:资料复用,重配策略表
            self._match_strategy(s)
            self._assemble_context(s)
            self._trace(s, "3 策略匹配(重配)", scene=s.strategy.scene.get("name"), platform=s.strategy.platform["name"])
            self._progress(progress, "发布策略已更新", f"重新适配{new_brief.scene}场景与{new_brief.platform}平台")
            s.content_plan = None
            return self.generate(s, progress=progress)
        # 换产品:资料、策略、稿件全部作废,从结构化需求重来
        s.evidence = s.strategy = s.context_package = s.content_plan = s.draft = s.review = None
        s.canonical_brief = ""
        # 可以重抽其他需求，但明确的最新型号不能被历史抽取结果覆盖。
        chosen_product = s.brief.product
        s.brief, _ = intake.next_step(self._llm("intake"), self.prompts, s.brief, s.history)
        s.brief.product = chosen_product
        questions = intake.validate(s.brief)
        self._trace(s, "1 结构化需求(重来)", brief=s.brief.__dict__, questions=questions)
        if questions:
            return Outcome("need_more_info", s, questions=questions)
        self._normalize_brief(s)
        return self.produce(s, progress=progress)

    def _block(self, s: Session, reason: str) -> Outcome:
        self._trace(s, "停止", reason=reason)
        return Outcome("blocked", s, reason=reason)

    # ---- 留存运行记录 ----
    def save(self, s: Session, name: str | None = None) -> Path:
        SAMPLES_DIR.mkdir(exist_ok=True)
        b = s.brief
        base = SAMPLES_DIR / (name or f"{time.strftime('%Y%m%d-%H%M%S')}-{b.scene}-{b.platform}")
        base.with_suffix(".json").write_text(json.dumps(s.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        if s.draft:
            base.with_suffix(".md").write_text(export.to_markdown(s), encoding="utf-8")
        return base
