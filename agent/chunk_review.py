"""内容块审核：确定性规则与单个审核模型共同给出重写任务。"""
from __future__ import annotations

import json

from . import briefing, compliance, fact_check
from .compose import draft_to_json, facts_block, images_block, originals_block
from .models import Brief, ContentBlock, Draft, Evidence, Issue, Review, Strategy
from .norm import normalize_where
from .prompts import Prompts


def _contract_issues(block: ContentBlock, draft: Draft) -> list[Issue]:
    issues: list[Issue] = []

    def add(where: str, detail: str, suggestion: str) -> None:
        issues.append(Issue("fact", "rule", "error", where, detail, suggestion))

    if block.kind == "opening" and (not draft.title or not draft.intro):
        add("当前块", "开篇块必须同时包含标题和导语", "补齐标题和导语")
    elif block.kind == "body" and not draft.body:
        add("当前块", "正文块没有正文段落", "围绕块目标生成至少一段正文")
    elif block.kind == "closing" and not draft.cta:
        add("当前块", "收束块缺少行动引导", "补充克制且符合平台规则的行动引导")

    allowed = set(block.fact_ids)
    for index, paragraph in enumerate(draft.body, 1):
        extra = [fact_id for fact_id in paragraph.fact_ids if fact_id not in allowed]
        if extra:
            add(f"正文第{index}段", f"使用了未分配给当前块的事实编号 {','.join(extra)}",
                "只使用当前块事实白名单，或删除相关主张")
    return issues


def parse_llm_issues(data: dict) -> list[Issue]:
    """把审核模型的结构化问题转为内部格式；兼容常见字段近义写法。"""
    output = []
    for item in data.get("issues", []) if isinstance(data, dict) else []:
        if not isinstance(item, dict):
            continue
        detail = item.get("detail") or item.get("problem")
        if not detail:
            continue
        stage = str(item.get("stage") or "fact").strip().lower()
        if stage not in ("fact", "compliance"):
            stage = "fact"
        output.append(Issue(
            stage, "llm", "error",
            normalize_where(str(item.get("where") or item.get("location") or "当前块")),
            str(detail), str(item.get("suggestion") or item.get("fix") or ""),
        ))
    return output


def check(
    llm,
    prompts: Prompts,
    block: ContentBlock,
    draft: Draft,
    evidence: Evidence,
    brief: Brief,
    strategy: Strategy,
    review_mode: str = "strict",
) -> Review:
    issues = _contract_issues(block, draft)
    issues += fact_check.rule_check(draft, evidence, brief, strategy)
    issues += compliance.rule_check(draft, evidence)
    data = llm.json(prompts.render("chunk_review", {
        "规范需求": briefing.canonicalize(brief),
        "块约束": json.dumps({
            "id": block.id, "kind": block.kind, "objective": block.objective,
            "fact_ids": block.fact_ids, "depends_on": block.depends_on,
            "version": block.version,
        }, ensure_ascii=False),
        "当前块稿件": draft_to_json(draft),
        "事实点清单": facts_block(evidence),
        "原始资料": originals_block(evidence),
        "图片登记": images_block(evidence, brief.product),
    }), temperature=0.1)
    issues += parse_llm_issues(data)
    if review_mode == "relaxed":
        for issue in issues:
            if issue.source == "llm":
                issue.severity = "warn"
    return Review(issues)


def check_coherence(llm, prompts: Prompts, before: Draft, after: Draft, evidence: Evidence) -> list[str]:
    """比较连贯性编辑前后语义事实；任何可疑变化都使整次编辑作废。"""
    data = llm.json(prompts.render("coherence_review", {
        "编辑前稿件": draft_to_json(before),
        "编辑后稿件": draft_to_json(after),
        "事实点清单": facts_block(evidence),
    }), temperature=0.0)
    if not isinstance(data, dict):
        return ["连贯性事实锁审核返回结构无效"]
    issues = [str(item.get("detail") or "事实内容发生变化")
              for item in data.get("issues", []) if isinstance(item, dict)]
    if data.get("changed") is True and not issues:
        issues.append("审核模型判定连贯性编辑改变了事实")
    return issues
