"""调度器:7 个阶段的串联与状态流转。

  1 结构化需求(多轮追问)→ 2 资料匹配 → 3 策略匹配 → 4 图文生成
  → 5 事实核验 + 6 合规检查(有问题则改写,最多 MAX_REWRITES 轮)→ 7 确认/修改

每个方法返回 Outcome,status 为:
  need_more_info  需要用户补充(questions)
  need_choice     型号无法确认,请用户从候选里选(questions + candidates)
  blocked         资料冲突或多轮改写后仍不达标,停止输出(reason)
  review          稿件已生成并通过核验,等待用户确认(阶段7)
  done            用户已确认
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import compliance, compose, evidence, export, fact_check, intake, prune, revise, strategy
from .knowledge import KnowledgeError
from .models import Brief, Draft, Issue, Review, Session
from .norm import norm_model
from .prompts import Prompts

MAX_REWRITES = 2
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
    def __init__(self, llm, store, prompts: Prompts, review_mode: str = "strict"):
        if review_mode not in ("strict", "relaxed"):
            raise ValueError("review_mode 必须为 strict 或 relaxed")
        self.llm = llm
        self.store = store
        self.prompts = prompts
        self.review_mode = "strict" if getattr(store, "requires_strict_review", False) else review_mode

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
            s.evidence = s.strategy = s.draft = s.review = None
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
        return self.produce(s, progress=progress)

    # ---- 阶段2-6:从需求到通过核验的稿件 ----
    def produce(
        self, s: Session, reuse_evidence: bool = False, progress: ProgressCallback | None = None
    ) -> Outcome:
        b = s.brief
        if not (reuse_evidence and s.evidence):
            out = self._match_evidence(s, progress)
            if out:
                return out
        s.strategy = strategy.match(b)                                         # 阶段3
        self._trace(s, "3 策略匹配", scene=s.strategy.scene.get("name"), platform=s.strategy.platform["name"])
        self._progress(
            progress, "策略已匹配",
            f"按{s.strategy.scene.get('name')}场景和{s.strategy.platform['name']}规则组织内容",
        )
        return self.generate(s, progress=progress)

    def _match_evidence(self, s: Session, progress: ProgressCallback | None = None) -> Outcome | None:
        b = s.brief
        self._progress(progress, "正在匹配知识库", f"只检索与“{b.product}”明确对应的资料和图片")
        try:
            if not self.store.connect()["connected"]:
                return self._block(s, f"本地知识库“{self.store.kb_name}”不可用，请检查 KB_LOCAL_DIR 和产品事实目录")
            collect = getattr(self.store, "collect", None)
            s.evidence = (collect(self._llm("evidence"), self._llm("image_review"), self.prompts, b, progress)
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

    # ---- 阶段4-6:生成 → 核验 → 合规 → 改写 → 自动删除 ----
    def _review(self, s: Session, draft: Draft) -> Review:
        issues = fact_check.check(self._llm("fact_review"), self.prompts, draft, s.evidence, s.brief, s.strategy)
        issues += compliance.check(self._llm("compliance_review"), self.prompts, draft, s.evidence)
        if self.review_mode == "relaxed":
            for issue in issues:
                if issue.source == "llm":
                    issue.severity = "warn"
        return Review(issues)

    def generate(self, s: Session, progress: ProgressCallback | None = None) -> Outcome:
        valid = getattr(self.store, "still_valid", None)
        if valid and s.evidence and not valid(s.evidence):
            out = self._match_evidence(s, progress)
            if out:
                return out
        self._progress(progress, "正在生成图文", "根据场景、平台规则和已确认事实组织内容")
        draft, refusals = compose.generate(self._llm("compose"), self.prompts, s.brief, s.evidence, s.strategy)
        draft = compose.fill_captions(draft, s.evidence)
        self._progress(
            progress, "初稿已生成",
            f"标题“{draft.title}”，正文 {len(draft.body)} 段，配图 {len(draft.images)} 张",
        )
        for round_no in range(MAX_REWRITES + 1):
            self._progress(progress, "正在核验内容", f"第 {round_no + 1} 轮检查产品事实、图片归属和营销合规")
            review = self._review(s, draft)
            self._trace(s, f"5-6 核验与合规(第{round_no + 1}轮)", errors=len(review.errors), warns=len(review.issues) - len(review.errors))
            if review.passed or round_no == MAX_REWRITES:
                break
            self._progress(progress, "正在修正问题", f"发现 {len(review.errors)} 项必须处理的问题，正在局部改写")
            text = "\n".join(f"- {i.where}:{i.detail}(要求:{i.suggestion})" for i in review.errors)
            draft, more = compose.rewrite(
                self._llm("rewrite"), self.prompts, draft, text, s.brief, s.evidence, s.strategy
            )
            draft = compose.fill_captions(draft, s.evidence)
            refusals = list(dict.fromkeys(refusals + more))

        removed: list[dict] = []
        if not review.passed:
            # 改写用尽仍有 error:按位置删掉有问题的内容并留痕,再复检一次
            max_images = s.strategy.platform.get("images", {}).get("max", 99)
            pruned, removed, unresolved = prune.apply(draft, review.errors, max_images)
            if unresolved:
                self._trace(s, "自动删除", result="有无法定位的问题,不删除", unresolved=[u.detail for u in unresolved])
            else:
                draft = pruned
                review = self._review(s, draft)
                self._trace(s, "自动删除", removed=removed, errors_after=len(review.errors))
        review.removed = removed
        s.draft, s.review = draft, review

        if not draft.body:
            return Outcome("blocked", s, reason="删除有问题的内容后正文已为空,无法输出,请人工处理", refusals=refusals)
        if not review.passed:
            return Outcome("blocked", s, reason="仍有无法自动处理的核验或合规问题,已停止输出,请人工处理", refusals=refusals)
        warn_count = len(review.issues) - len(review.errors)
        self._progress(progress, "生成完成", f"阻断项已通过；{warn_count} 项提示可在右侧核验卡查看")
        return Outcome("review", s, refusals=refusals)

    # ---- 阶段7:确认与修改 ----
    def feedback(self, s: Session, user_text: str, progress: ProgressCallback | None = None) -> Outcome:
        s.history.append({"role": "用户", "content": user_text})
        self._progress(progress, "正在理解修改要求", "判断需要调整文案、平台、场景还是产品")
        intent = revise.classify(self._llm("revise_intent"), self.prompts, s.brief, s.draft, user_text)
        self._trace(s, "7 修改意图", **intent)
        new_brief, step = revise.apply(s.brief, intent, user_text)
        if new_brief is None:
            q = "没太明白你想改什么。是想换场景、换平台、换产品,还是只是调整文案?"
            s.history.append({"role": "助手", "content": q})
            return Outcome("need_more_info", s, questions=[q])
        s.brief = new_brief
        if step == "done":
            self._progress(progress, "定稿完成", "当前版本已确认，可下载发布成品或完整报告")
            return Outcome("done", s)
        if step == "generate":                  # 只重写文案:需求、资料、策略都复用
            self._progress(progress, "修改范围已确认", "复用已核验资料，只重新生成文案")
            return self.generate(s, progress=progress)
        if step == "strategy":                  # 换场景/平台:资料复用,重配策略表
            s.strategy = strategy.match(new_brief)
            self._trace(s, "3 策略匹配(重配)", scene=s.strategy.scene.get("name"), platform=s.strategy.platform["name"])
            self._progress(progress, "发布策略已更新", f"重新适配{new_brief.scene}场景与{new_brief.platform}平台")
            return self.generate(s, progress=progress)
        # 换产品:资料、策略、稿件全部作废,从结构化需求重来
        s.evidence = s.strategy = s.draft = s.review = None
        # 可以重抽其他需求，但明确的最新型号不能被历史抽取结果覆盖。
        chosen_product = s.brief.product
        s.brief, _ = intake.next_step(self._llm("intake"), self.prompts, s.brief, s.history)
        s.brief.product = chosen_product
        questions = intake.validate(s.brief)
        self._trace(s, "1 结构化需求(重来)", brief=s.brief.__dict__, questions=questions)
        if questions:
            return Outcome("need_more_info", s, questions=questions)
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
