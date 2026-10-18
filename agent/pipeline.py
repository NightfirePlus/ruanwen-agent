"""调度器:串起 8 个环节,返回 need_more_info / blocked / done 三种状态。"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import compliance, compose, config, export, facts as facts_mod, intake, scene, verify
from .models import Fact, Issue, Result, Task
from .retrieve import Retriever, norm

MAX_REVISIONS = 2
SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"


class Pipeline:
    def __init__(self, llm, retriever: Retriever):
        self.llm = llm
        self.retriever = retriever

    def _trace(self, r: Result, step: str, **info) -> None:
        r.trace.append({"step": step, "at": time.strftime("%H:%M:%S"), **info})

    def run(self, raw: Task) -> Result:
        # 1 任务接收:缺项直接追问
        questions = intake.check(raw)
        if questions:
            r = Result(status="need_more_info", task=raw, questions=questions)
            self._trace(r, "任务接收", result="信息不足", questions=questions)
            return r
        task = intake.normalize(raw)
        r = Result(status="done", task=task)
        self._trace(r, "任务接收", result="信息齐全", task=task.__dict__)

        # 2 场景解析
        r.scene_judgement = scene.parse(self.llm, task)
        self._trace(r, "场景解析", result=r.scene_judgement)

        # 3 策略匹配
        r.strategy = scene.build_strategy(task, r.scene_judgement)
        self._trace(r, "策略匹配", angle=r.strategy["angle"], platform=task.platform)

        # 4 检索
        if not self.retriever.kb_id:
            status = self.retriever.connect()
            if not status["connected"]:
                return self._block(r, f"未找到知识库“{status['kb_name']}”,请确认已用同一账号加入共享知识库")
        docs = self.retriever.find_docs(task.product)
        if not docs:
            return self._block(r, f"知识库中没有找到型号“{task.product}”的资料,请确认型号后重试")
        self._trace(r, "检索资料", docs=[d["title"] for d in docs])

        # 5 事实提取(逐篇资料,摘录必须存在于原文)
        conflicts: list[str] = []
        for d in docs[:4]:
            content = self.retriever.read_doc(d["media_id"])
            if not content.strip():
                continue
            fs, cf = facts_mod.extract(self.llm, task.product, d["title"], d["media_id"], content, len(r.facts) + 1)
            r.facts += fs
            conflicts += cf
        if not r.facts:
            return self._block(r, "未能从知识库资料中提取到可用的事实点,无法生成有依据的内容")
        conflicts += verify.detect_conflicts(r.facts)
        self._trace(r, "事实提取", count=len(r.facts), conflicts=conflicts)
        if conflicts:
            return self._block(r, "知识库资料存在冲突,请人工确认后再生成:" + ";".join(conflicts))

        r.images = self.retriever.find_images(task.product)
        usable = [i for i in r.images if norm(i.model) == norm(task.product)]
        self._trace(r, "检索图片", total=len(r.images), matched=len(usable))

        # 6 图文生成 → 7 核验 → 8 合规,发现问题则修订
        draft, refusals = compose.generate(self.llm, task, r.scene_judgement, r.strategy, r.facts, usable)
        r.refusals = refusals
        limits = r.strategy["platform_rules"]["images"]
        for round_no in range(MAX_REVISIONS + 1):
            issues = verify.check(draft, r.facts, usable, task.product, limits) + compliance.check(draft, r.facts)
            errors = [i for i in issues if i.severity == "error"]
            self._trace(r, f"核验与合规(第{round_no + 1}轮)", errors=len(errors), warns=len(issues) - len(errors))
            if not errors or round_no == MAX_REVISIONS:
                r.issues = issues
                break
            text = "\n".join(f"- {i.where}:{i.detail}(要求:{i.action})" for i in errors)
            draft, more = compose.revise(self.llm, task, r.scene_judgement, r.strategy, r.facts, usable, draft, text)
            r.refusals = list(dict.fromkeys(r.refusals + more))
        r.draft = draft

        if any(i.severity == "error" for i in r.issues):
            r.status = "blocked"
            r.reason = "多轮修订后仍有未解决的核验或合规问题,已停止输出,请人工处理"
        return r

    def _block(self, r: Result, reason: str) -> Result:
        r.status = "blocked"
        r.reason = reason
        self._trace(r, "停止", reason=reason)
        return r

    # ---- 留存运行记录,用于说明书和复赛 ----
    def save_sample(self, r: Result, name: str | None = None) -> Path:
        SAMPLES_DIR.mkdir(exist_ok=True)
        stem = name or f"{time.strftime('%Y%m%d-%H%M%S')}-{r.task.scene}-{r.task.platform}"
        base = SAMPLES_DIR / stem
        base.with_suffix(".json").write_text(json.dumps(r.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        if r.status == "done":
            base.with_suffix(".md").write_text(export.to_markdown(r), encoding="utf-8")
        return base
