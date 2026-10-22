"""生成/重写模型与审核模型之间的双队列分块流水线。"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from . import chunk_review, compose, prune
from .models import Brief, ContentBlock, ContentPlan, Draft, Evidence, Review, Strategy
from .prompts import Prompts

MAX_REWRITES = 2
ProgressCallback = Callable[[str, str], None]


@dataclass
class WorkItem:
    action: str
    block_id: str
    version: int
    queued_at: float = field(default_factory=time.monotonic)


@dataclass
class ReviewItem:
    block_id: str
    version: int
    draft: Draft
    queued_at: float = field(default_factory=time.monotonic)


@dataclass
class Event:
    kind: str
    block_id: str
    version: int
    draft: Draft | None = None
    review: Review | None = None
    refusals: list[str] = field(default_factory=list)
    error: Exception | None = None
    seconds: float = 0.0
    wait_seconds: float = 0.0


@dataclass
class FlowResult:
    draft: Draft | None
    refusals: list[str]
    error: str = ""
    events: list[dict] = field(default_factory=list)
    removed: list[dict] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


def run(
    writer_llm,
    reviewer_llm,
    prompts: Prompts,
    canonical_brief: str,
    brief: Brief,
    evidence: Evidence,
    strategy: Strategy,
    plan: ContentPlan,
    review_mode: str = "strict",
    progress: ProgressCallback | None = None,
) -> FlowResult:
    """运行两个单消费者队列，使写作和审核在不同内容块上重叠执行。"""
    writer_queue: queue.Queue[WorkItem | None] = queue.Queue()
    review_queue: queue.Queue[ReviewItem | None] = queue.Queue()
    events: queue.Queue[Event] = queue.Queue()
    stop = threading.Event()
    by_id = {block.id: block for block in plan.blocks}
    flow_events: list[dict] = []
    all_refusals: list[str] = []
    all_removed: list[dict] = [item for block in plan.blocks for item in block.removed]
    pruned_blocks: set[str] = set()
    flow_started = time.monotonic()
    first_passed_seconds: float | None = None
    writer_seconds = reviewer_seconds = 0.0
    writer_wait_seconds = reviewer_wait_seconds = 0.0
    generated_versions = reviewed_versions = rewrites = 0

    def passed_blocks() -> list[ContentBlock]:
        return [block for block in plan.blocks if block.status == "passed"]

    def writer() -> None:
        while not stop.is_set():
            item = writer_queue.get()
            if item is None:
                return
            block = by_id[item.block_id]
            started = time.monotonic()
            waited = started - item.queued_at
            try:
                if item.action == "rewrite":
                    issues_text = "\n".join(
                        f"- {issue.where}：{issue.detail}（要求：{issue.suggestion}）"
                        for issue in block.issues if issue.severity == "error"
                    )
                    draft, refusals = compose.rewrite_chunk(
                        writer_llm, prompts, canonical_brief, brief, evidence, strategy,
                        plan, block, passed_blocks(), issues_text,
                    )
                else:
                    draft, refusals = compose.generate_chunk(
                        writer_llm, prompts, canonical_brief, brief, evidence, strategy,
                        plan, block, passed_blocks(),
                    )
                events.put(Event("written", block.id, item.version, draft=draft,
                                 refusals=refusals, seconds=time.monotonic() - started,
                                 wait_seconds=waited))
            except Exception as exc:  # 传回主调度线程统一关闭队列并阻断。
                events.put(Event("error", block.id, item.version, error=exc,
                                 seconds=time.monotonic() - started, wait_seconds=waited))

    def reviewer() -> None:
        while not stop.is_set():
            item = review_queue.get()
            if item is None:
                return
            block = by_id[item.block_id]
            started = time.monotonic()
            waited = started - item.queued_at
            try:
                review = chunk_review.check(
                    reviewer_llm, prompts, block, item.draft, evidence, brief, strategy, review_mode,
                )
                events.put(Event("reviewed", item.block_id, item.version, review=review,
                                 seconds=time.monotonic() - started, wait_seconds=waited))
            except Exception as exc:
                events.put(Event("error", item.block_id, item.version, error=exc,
                                 seconds=time.monotonic() - started, wait_seconds=waited))

    writer_thread = threading.Thread(target=writer, name="content-writer", daemon=True)
    reviewer_thread = threading.Thread(target=reviewer, name="content-reviewer", daemon=True)
    writer_thread.start()
    reviewer_thread.start()

    def emit(title: str, detail: str) -> None:
        if progress:
            progress(title, detail)

    def schedule_ready() -> int:
        count = 0
        for block in plan.blocks:
            if block.status != "pending_generate":
                continue
            if not all(by_id[dep].status == "passed" for dep in block.depends_on):
                continue
            block.status = "generating"
            block.attempts += 1
            block.version += 1
            writer_queue.put(WorkItem("generate", block.id, block.version))
            flow_events.append({"event": "queued_generate", "block": block.id, "version": block.version})
            emit("内容块进入生成队列", f"{block.id} · {block.objective}")
            count += 1
        return count

    schedule_ready()
    failure = ""
    try:
        while not all(block.status == "passed" for block in plan.blocks):
            try:
                event = events.get(timeout=0.25)
            except queue.Empty:
                active = any(block.status in ("generating", "reviewing") for block in plan.blocks)
                if not active and not schedule_ready():
                    failure = "内容块依赖无法满足，流水线已停止"
                    break
                continue
            block = by_id[event.block_id]
            if event.version != block.version:
                flow_events.append({"event": "stale_result_ignored", "block": block.id,
                                    "version": event.version, "current_version": block.version})
                continue
            if event.kind == "error":
                block.status = "blocked"
                failure = f"内容块 {block.id} 处理失败：{event.error}"
                flow_events.append({"event": "error", "block": block.id,
                                    "version": block.version, "error": str(event.error)})
                break
            if event.kind == "written":
                generated_versions += 1
                writer_seconds += event.seconds
                writer_wait_seconds += event.wait_seconds
                block.draft = event.draft
                all_refusals.extend(event.refusals)
                block.status = "reviewing"
                review_queue.put(ReviewItem(block.id, block.version, event.draft or Draft()))
                flow_events.append({"event": "written", "block": block.id,
                                    "version": block.version, "seconds": round(event.seconds, 3),
                                    "wait_seconds": round(event.wait_seconds, 3)})
                emit("内容块进入审核队列", f"{block.id} 第 {block.version} 版已生成，等待事实与合规审核")
                schedule_ready()
                continue

            review = event.review or Review([])
            reviewed_versions += 1
            reviewer_seconds += event.seconds
            reviewer_wait_seconds += event.wait_seconds
            block.issues = review.issues
            errors = review.errors
            flow_events.append({"event": "reviewed", "block": block.id,
                                "version": block.version, "seconds": round(event.seconds, 3),
                                "wait_seconds": round(event.wait_seconds, 3),
                                "errors": len(errors), "warnings": len(review.issues) - len(errors)})
            if not errors:
                block.status = "passed"
                if first_passed_seconds is None:
                    first_passed_seconds = time.monotonic() - flow_started
                emit("内容块审核通过", f"{block.id} 第 {block.version} 版已冻结")
                schedule_ready()
                continue
            if block.attempts <= MAX_REWRITES:
                block.status = "generating"
                block.attempts += 1
                block.version += 1
                rewrites += 1
                writer_queue.put(WorkItem("rewrite", block.id, block.version))
                flow_events.append({"event": "queued_rewrite", "block": block.id,
                                    "version": block.version, "errors": len(errors)})
                emit("问题块返回重写队列", f"{block.id} 有 {len(errors)} 项必须修正的问题")
            else:
                max_images = strategy.platform.get("images", {}).get("max", 99)
                pruned, removed, unresolved = prune.apply(block.draft or Draft(), errors, max_images)
                if removed and not unresolved and block.id not in pruned_blocks:
                    block.removed.extend(removed)
                    all_removed.extend(removed)
                    pruned_blocks.add(block.id)
                    block.version += 1
                    block.draft = pruned
                    if block.kind == "body" and not pruned.body:
                        block.status = "blocked"
                        failure = f"内容块 {block.id} 删除问题内容后正文已为空，流水线已停止"
                        break
                    block.status = "reviewing"
                    review_queue.put(ReviewItem(block.id, block.version, pruned))
                    flow_events.append({"event": "pruned", "block": block.id,
                                        "version": block.version, "removed": removed})
                    emit("问题块执行安全删除", f"{block.id} 无法改写的内容已删除并重新审核")
                else:
                    block.status = "blocked"
                    failure = f"内容块 {block.id} 达到重写上限后仍未通过审核"
                    emit("内容块已阻断", failure)
                    break
    finally:
        stop.set()
        writer_queue.put(None)
        review_queue.put(None)
        writer_thread.join()
        reviewer_thread.join()
        if failure:
            # 其他块可能仍在模型调用或审核队列中；结果未被主调度确认时不得保留为
            # generating/reviewing，避免后续局部重试永远无法重新调度这些块。
            for block in plan.blocks:
                if block.status in ("generating", "reviewing"):
                    block.status = "pending_generate"

    metrics = {
        "first_block_passed_seconds": (
            round(first_passed_seconds, 3) if first_passed_seconds is not None else None
        ),
        "total_seconds": round(time.monotonic() - flow_started, 3),
        "writer_processing_seconds": round(writer_seconds, 3),
        "reviewer_processing_seconds": round(reviewer_seconds, 3),
        "writer_queue_wait_seconds": round(writer_wait_seconds, 3),
        "reviewer_queue_wait_seconds": round(reviewer_wait_seconds, 3),
        "generated_versions": generated_versions,
        "reviewed_versions": reviewed_versions,
        "rewrites": rewrites,
        "rewrite_rate": round(rewrites / generated_versions, 4) if generated_versions else 0.0,
    }
    return FlowResult(
        draft=None if failure else compose.assemble(plan),
        refusals=list(dict.fromkeys(all_refusals)),
        error=failure,
        events=flow_events,
        removed=all_removed,
        metrics=metrics,
    )
