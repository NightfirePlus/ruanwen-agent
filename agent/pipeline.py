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

from . import compliance, compose, evidence, export, fact_check, intake, revise, strategy
from .models import Brief, Draft, Issue, Review, Session
from .prompts import Prompts

MAX_REWRITES = 2
SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"


@dataclass
class Outcome:
    status: str
    session: Session
    questions: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    reason: str = ""
    refusals: list[str] = field(default_factory=list)


class Pipeline:
    def __init__(self, llm, store: evidence.KnowledgeStore, prompts: Prompts):
        self.llm = llm
        self.store = store
        self.prompts = prompts

    # ---- 记录 ----
    @staticmethod
    def _trace(s: Session, stage: str, **info) -> None:
        s.trace.append({"stage": stage, "at": time.strftime("%H:%M:%S"), **info})

    # ---- 阶段1:多轮追问 ----
    def talk(self, s: Session, user_text: str) -> Outcome:
        """处理一轮用户输入。信息不完整返回 need_more_info;完整后自动进入后续阶段。"""
        s.history.append({"role": "用户", "content": user_text})
        s.brief, questions = intake.next_step(self.llm, self.prompts, s.brief, s.history)
        self._trace(s, "1 结构化需求", brief=s.brief.__dict__, questions=questions)
        if questions:
            s.history.append({"role": "助手", "content": "\n".join(questions)})
            return Outcome("need_more_info", s, questions=questions)
        return self.produce(s)

    # ---- 阶段2-6:从需求到通过核验的稿件 ----
    def produce(self, s: Session, reuse_evidence: bool = False) -> Outcome:
        b = s.brief
        if not (reuse_evidence and s.evidence):
            out = self._match_evidence(s)
            if out:
                return out
        s.strategy = strategy.match(b)                                         # 阶段3
        self._trace(s, "3 策略匹配", scene=s.strategy.scene.get("name"), platform=s.strategy.platform["name"])
        return self.generate(s)

    def _match_evidence(self, s: Session) -> Outcome | None:
        b = s.brief
        if not self.store.kb_id and not self.store.connect()["connected"]:
            return self._block(s, f"未找到知识库“{self.store.kb_name}”,请确认已用同一账号加入共享知识库")
        try:
            s.evidence = evidence.build(self.llm, self.prompts, self.store, b.product)   # 阶段2
        except evidence.ModelNotResolved as e:
            self._trace(s, "2 资料匹配", result="型号未确认", candidates=e.candidates)
            hint = f"知识库里没有型号“{e.model}”。" + (f"你是想推广其中之一吗?{'、'.join(e.candidates)}" if e.candidates else "请确认型号。")
            return Outcome("need_choice", s, questions=[hint], candidates=e.candidates)
        ev = s.evidence
        self._trace(
            s, "2 资料匹配", facts=len(ev.facts), images=len(ev.images), sources=ev.sources,
            dropped=ev.dropped, conflicts=ev.conflicts, similar_models=ev.candidates,
        )
        if ev.conflicts:
            return self._block(s, "知识库资料存在冲突,请人工确认后再生成:" + ";".join(ev.conflicts))
        if not ev.facts:
            return self._block(s, "未能从知识库资料中提取到可用的事实点,无法生成有依据的内容")
        return None

    # ---- 阶段4-6:生成 → 核验 → 合规 → 改写 ----
    def generate(self, s: Session) -> Outcome:
        draft, refusals = compose.generate(self.llm, self.prompts, s.brief, s.evidence, s.strategy)
        for round_no in range(MAX_REWRITES + 1):
            issues = fact_check.check(self.llm, self.prompts, draft, s.evidence, s.brief, s.strategy)
            issues += compliance.check(self.llm, self.prompts, draft, s.evidence)
            review = Review(issues)
            self._trace(s, f"5-6 核验与合规(第{round_no + 1}轮)", errors=len(review.errors), warns=len(issues) - len(review.errors))
            if review.passed or round_no == MAX_REWRITES:
                break
            text = "\n".join(f"- {i.where}:{i.detail}(要求:{i.suggestion})" for i in review.errors)
            draft, more = compose.rewrite(self.llm, self.prompts, draft, text)
            refusals = list(dict.fromkeys(refusals + more))
        s.draft, s.review = draft, review
        if not review.passed:
            return Outcome("blocked", s, reason="多轮改写后仍有未解决的核验或合规问题,已停止输出,请人工处理", refusals=refusals)
        return Outcome("review", s, refusals=refusals)

    # ---- 阶段7:确认与修改 ----
    def feedback(self, s: Session, user_text: str) -> Outcome:
        intent = revise.classify(self.llm, self.prompts, s.brief, s.draft, user_text)
        self._trace(s, "7 修改意图", **intent)
        new_brief, step = revise.apply(s.brief, intent, user_text)
        if new_brief is None:
            q = "没太明白你想改什么。是想换场景、换平台、换产品,还是只是调整文案?"
            s.history.append({"role": "助手", "content": q})
            return Outcome("need_more_info", s, questions=[q])
        s.brief = new_brief
        if step == "done":
            return Outcome("done", s)
        if step == "generate":                  # 只重写文案:需求、资料、策略都复用
            return self.generate(s)
        if step == "strategy":                  # 换场景/平台:资料复用,重配策略表
            s.strategy = strategy.match(new_brief)
            self._trace(s, "3 策略匹配(重配)", scene=s.strategy.scene.get("name"), platform=s.strategy.platform["name"])
            return self.generate(s)
        # 换产品:资料、策略、稿件全部作废,从结构化需求重来
        s.evidence = s.strategy = s.draft = s.review = None
        s.brief, questions = intake.next_step(self.llm, self.prompts, s.brief, s.history)
        self._trace(s, "1 结构化需求(重来)", brief=s.brief.__dict__, questions=questions)
        if questions:
            return Outcome("need_more_info", s, questions=questions)
        return self.produce(s)

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
