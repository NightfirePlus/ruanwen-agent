"""根据规范任务说明和当前事实生成、校验文章分块计划。"""
from __future__ import annotations

import json

from .compose import facts_block
from .models import ContentBlock, ContentPlan, Evidence, Strategy
from .prompts import Prompts

KINDS = ("opening", "body", "closing")
MAX_BLOCKS = 8

STRUCTURE_REQUIREMENTS = {
    "opening": "必须同时生成非空标题和非空导语；只写规范需求中的受众、场景与关注问题，不写产品参数、功能或效果",
    "body": "必须生成至少一个正文段落",
    "closing": "必须生成非空行动引导，并按平台要求生成标签；不新增产品参数、功能、效果、权益或承诺",
}


def _with_structure_requirement(kind: str, objective: str) -> str:
    requirement = STRUCTURE_REQUIREMENTS[kind]
    return objective if requirement in objective else f"{objective}；{requirement}"


def _fallback(fact_ids: list[str]) -> ContentPlan:
    return ContentPlan([
        ContentBlock("B1", "opening", _with_structure_requirement(
            "opening", "形成标题和导语，点明受众与场景"), []),
        ContentBlock("B2", "body", _with_structure_requirement(
            "body", "围绕核心问题展开有事实依据的主要卖点"), fact_ids[:2]),
        ContentBlock("B3", "closing", _with_structure_requirement(
            "closing", "收束全文并给出克制的行动引导"), [], ["B1", "B2"]),
    ])


def parse_plan(data: dict, evidence: Evidence) -> ContentPlan:
    allowed_facts = {fact.id for fact in evidence.facts}
    raw_blocks = data.get("blocks", []) if isinstance(data, dict) else []
    blocks: list[ContentBlock] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_blocks[:MAX_BLOCKS], 1):
        if not isinstance(item, dict):
            continue
        block_id = str(item.get("id") or f"B{index}").strip()
        kind = str(item.get("kind") or "body").strip().lower()
        objective = str(item.get("objective") or "").strip()
        if not block_id or block_id in seen or kind not in KINDS or not objective:
            continue
        fact_ids = list(dict.fromkeys(str(value) for value in item.get("fact_ids", [])
                                      if str(value) in allowed_facts))
        # 标题、导语和收束没有段落级 fact_ids 载体，不在这些块中承载产品事实；
        # 正文块控制事实密度，避免一段同时压入过多限定条件而造成遗漏；上限保留
        # 三条，以免机械截断后让模型规划的目标与事实白名单互相矛盾。
        fact_ids = fact_ids[:3] if kind == "body" else []
        depends_on = list(dict.fromkeys(str(value) for value in item.get("depends_on", [])
                                        if str(value) in seen))
        blocks.append(ContentBlock(
            block_id, kind, _with_structure_requirement(kind, objective), fact_ids, depends_on
        ))
        seen.add(block_id)
    if (not blocks or not any(block.kind == "opening" for block in blocks)
            or not any(block.kind == "body" for block in blocks)
            or not any(block.kind == "closing" for block in blocks)):
        return _fallback([fact.id for fact in evidence.facts])
    # 每种结构只保留第一个 opening/closing；正文块可有多个。规划顺序保持不变。
    opening_seen = closing_seen = False
    normalized = []
    for block in blocks:
        if block.kind == "opening":
            if opening_seen:
                block.kind = "body"
            opening_seen = True
        elif block.kind == "closing":
            if closing_seen:
                block.kind = "body"
            closing_seen = True
        normalized.append(block)
    return ContentPlan(normalized)


def create(llm, prompts: Prompts, canonical_brief: str, evidence: Evidence, strategy: Strategy) -> ContentPlan:
    data = llm.json(
        prompts.render(
            "content_plan",
            {
                "规范需求": canonical_brief,
                "策略": json.dumps(
                    {"scene": strategy.scene, "platform": strategy.platform,
                     "performance_reference": strategy.performance},
                    ensure_ascii=False,
                ),
                "事实点清单": facts_block(evidence),
            },
        ),
        temperature=0.2,
    )
    return parse_plan(data, evidence)
