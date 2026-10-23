"""阶段4:综合需求、策略、事实点与图片,生成图文软文。提示词见 prompts/compose.md、writer_system.md。"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import fields

from . import section
from .models import Brief, ContentBlock, ContentPlan, ContextPackage, Draft, Evidence, ImagePlacement, Paragraph, Strategy
from .norm import usable_images
from .prompts import Prompts


def facts_block(ev: Evidence, package: ContextPackage | None = None) -> str:
    facts = package.facts if package else ev.facts
    return "\n".join(f"[{f.id}] ({f.model}) {f.text}  〔来源:{f.source_path}〕" for f in facts)


def images_block(
    ev: Evidence, model: str, package: ContextPackage | None = None,
    exclude: set[str] | None = None,
) -> str:
    image_source = Evidence(images=package.images) if package else ev
    imgs = [i for i in usable_images(image_source, model) if not exclude or i.id not in exclude]
    return "\n".join(json.dumps({"id": i.id, "model": i.model, "name": i.name,
        "source": i.source_path, "registered_caption": i.caption, "usage": i.usage,
        "note": i.note, "basis": "知识库已登记并绑定型号；未执行视觉模型审图"},
        ensure_ascii=False) for i in imgs) or "(无)"


def used_image_ids(passed: list[ContentBlock]) -> set[str]:
    """已经写进通过块的图片，后续块不再重复提供。"""
    return {placement.image_id for block in passed if block.draft
            for placement in block.draft.images}


BUCKET_LABELS = {
    "platform": "平台写法：该平台的语气、结构与图文节奏",
    "copy": "成稿写法：可直接模仿的篇幅、段落节奏与句式",
    "audience": "人群场景：受众顾虑、使用任务与推荐边界",
}


def _label_references(references: list[dict]) -> list[dict]:
    """给每份参考标出用途，便于写作模型区分该学语气、学结构还是学受众语言。"""
    return [dict(item, usage=BUCKET_LABELS.get(item.get("bucket", ""), "表达与场景参考")) for item in references]


def context_block(ev: Evidence, package: ContextPackage | None = None) -> str:
    references = package.expression_references if package else ev.context
    payload = {
        "references": _label_references(references),
        "source": "知识库表达参考全文摘录，仅用于学习写法与人群语言；其中的参数、功能、价格、体验不得作为本轮产品事实",
        "authority": (
            package.authority.get("expression_references")
            if package else "仅影响表达、结构和场景，不得升级为产品事实"
        ),
    }
    return json.dumps(payload, ensure_ascii=False)


def strategy_block(strategy: Strategy, package: ContextPackage | None = None) -> str:
    payload = package.strategy_payload() if package else {
        "scene": strategy.scene,
        "platform": strategy.platform,
        "performance_reference": strategy.performance,
    }
    return json.dumps(payload, ensure_ascii=False)


def originals_block(ev: Evidence) -> str:
    return json.dumps(ev.originals, ensure_ascii=False)


def brief_block(b: Brief) -> str:
    return json.dumps({f.name: getattr(b, f.name) for f in fields(Brief)}, ensure_ascii=False)


def draft_to_json(d: Draft) -> str:
    return json.dumps(
        {
            "title": d.title,
            "intro": d.intro,
            "body": [{"text": p.text, "fact_ids": p.fact_ids} for p in d.body],
            "cta": d.cta,
            "tags": d.tags,
            "images": [im.__dict__ for im in d.images],
        },
        ensure_ascii=False,
    )


def plan_to_json(plan: ContentPlan) -> str:
    return json.dumps(
        {"blocks": [{
            "id": block.id,
            "kind": block.kind,
            "objective": block.objective,
            "fact_ids": block.fact_ids,
            "depends_on": block.depends_on,
        } for block in plan.blocks]},
        ensure_ascii=False,
    )


def parse_draft(data: dict) -> tuple[Draft, list[str]]:
    """把模型输出解析成 Draft,同时返回模型声明拒绝写入的内容。"""
    body = []
    for p in data.get("body", []):
        if isinstance(p, dict):
            body.append(Paragraph(str(p.get("text", "")).strip(), [str(x) for x in p.get("fact_ids") or []]))
        else:
            body.append(Paragraph(str(p).strip()))
    images = []
    for im in data.get("images", []):
        try:
            images.append(
                ImagePlacement(
                    str(im["image_id"]), int(im.get("after_paragraph", 0)), str(im.get("caption", "")), str(im.get("reason", ""))
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    draft = Draft(
        title=str(data.get("title", "")).strip(),
        intro=str(data.get("intro", "")).strip(),
        body=[p for p in body if p.text],
        cta=str(data.get("cta", "")).strip(),
        tags=[str(t) for t in data.get("tags", []) if t],
        images=images,
    )
    return draft, [str(r) for r in data.get("refusals", []) if r]


def fill_captions(draft: Draft, ev: Evidence) -> Draft:
    """图注以知识库登记说明为准:登记过说明的图片一律使用登记原文。

    模型改写图注会引入措辞漂移(例如把“原野绿多彩配色”写成“原野绿配色”),
    核验环节会判定为改变原意,最终整张图被自动删除。登记说明本身是已核验文案,
    直接采用既安全又能保留图片;只有没有登记说明时才保留模型写的图注。
    """
    registered = {i.id: i.caption for i in ev.images if i.caption and i.caption.strip()}
    for pl in draft.images:
        if pl.image_id in registered:
            pl.caption = registered[pl.image_id]
    return draft


def restrict_to_block(draft: Draft, block: ContentBlock) -> Draft:
    """丢弃模型越权生成的其他结构字段，保证块之间职责不重叠。"""
    if block.kind == "opening":
        return Draft(title=draft.title, intro=draft.intro)
    if block.kind == "closing":
        return Draft(cta=draft.cta, tags=list(draft.tags))
    return Draft(body=list(draft.body), images=list(draft.images))


def chunk_length(platform: dict, block: ContentBlock, plan: ContentPlan) -> str:
    """当前块应达到的篇幅;非正文块返回空串。"""
    if block.kind != "body":
        return ""
    expected = section.expected_chars(platform, block.kind, section.body_blocks(plan))
    if not expected:
        return ""
    return f"约{expected}字，至少{section.paragraphs_per_block(platform, section.body_blocks(plan))}段"


def _chunk_values(
    canonical_brief: str,
    strategy: Strategy,
    plan: ContentPlan,
    block: ContentBlock,
    passed: list[ContentBlock],
    ev: Evidence,
    context_package: ContextPackage | None = None,
) -> dict[str, str]:
    return {
        "规范需求": canonical_brief,
        "策略": strategy_block(strategy, context_package),
        "内容计划": plan_to_json(plan),
        "当前块": json.dumps({
            "id": block.id, "kind": block.kind, "objective": block.objective,
            "fact_ids": block.fact_ids, "depends_on": block.depends_on,
            "version": block.version,
            "篇幅": chunk_length(strategy.platform, block, plan),
        }, ensure_ascii=False),
        "已通过块": json.dumps([
            {"id": item.id, "kind": item.kind,
             "draft": json.loads(draft_to_json(item.draft)) if item.draft else {}}
            for item in passed
        ], ensure_ascii=False),
        "事实点清单": facts_block(ev, context_package),
        "可用图片": images_block(
            ev,
            (context_package.facts[0].model if context_package and context_package.facts
             else ev.facts[0].model if ev.facts else ""),
            context_package,
            exclude=used_image_ids(passed),
        ),
        "表达参考": context_block(ev, context_package),
    }


def generate_chunk(
    llm,
    prompts: Prompts,
    canonical_brief: str,
    brief: Brief,
    ev: Evidence,
    strategy: Strategy,
    plan: ContentPlan,
    block: ContentBlock,
    passed: list[ContentBlock],
    context_package: ContextPackage | None = None,
) -> tuple[Draft, list[str]]:
    values = _chunk_values(canonical_brief, strategy, plan, block, passed, ev, context_package)
    values["可用图片"] = images_block(ev, brief.product, context_package, exclude=used_image_ids(passed))
    draft, refusals = parse_draft(llm.json(
        prompts.render("chunk_generate", values),
        system=prompts.writer_system,
        temperature=0.7,
    ))
    return fill_captions(restrict_to_block(draft, block), ev), refusals


def rewrite_chunk(
    llm,
    prompts: Prompts,
    canonical_brief: str,
    brief: Brief,
    ev: Evidence,
    strategy: Strategy,
    plan: ContentPlan,
    block: ContentBlock,
    passed: list[ContentBlock],
    issues_text: str,
    context_package: ContextPackage | None = None,
) -> tuple[Draft, list[str]]:
    values = _chunk_values(canonical_brief, strategy, plan, block, passed, ev, context_package)
    values.update({
        "可用图片": images_block(ev, brief.product, context_package, exclude=used_image_ids(passed)),
        "当前块稿件": draft_to_json(block.draft or Draft()),
        "审核问题": issues_text,
    })
    draft, refusals = parse_draft(llm.json(
        prompts.render("chunk_rewrite", values),
        system=prompts.writer_system,
        temperature=0.4,
    ))
    return fill_captions(restrict_to_block(draft, block), ev), refusals


def assemble(plan: ContentPlan) -> Draft:
    """按计划顺序合并所有已通过块，并把块内图片位置换算成全文位置。"""
    result = Draft()
    body_offset = 0
    for block in plan.blocks:
        fragment = block.draft or Draft()
        if block.kind == "opening":
            result.title, result.intro = fragment.title, fragment.intro
        elif block.kind == "closing":
            result.cta, result.tags = fragment.cta, list(fragment.tags)
        else:
            for image in fragment.images:
                copied = deepcopy(image)
                copied.after_paragraph += body_offset
                result.images.append(copied)
            result.body.extend(deepcopy(fragment.body))
            body_offset = len(result.body)
    return result


_NUMBER_TOKEN = re.compile(r"\d+(?:\.\d+)?(?:\s*[A-Za-z%]+|\s*(?:英寸|寸|小时|分钟|瓦|克|毫米|倍|代))?", re.I)


def fact_lock_violations(before: Draft, after: Draft) -> list[str]:
    """检查连贯性编辑是否改变结构化事实载体；违反时整次编辑作废。"""
    problems = []
    if len(before.body) != len(after.body):
        problems.append("正文段落数量发生变化")
    else:
        for index, (old, new) in enumerate(zip(before.body, after.body), 1):
            if old.fact_ids != new.fact_ids:
                problems.append(f"正文第{index}段事实编号发生变化")
    old_images = [(im.image_id, im.after_paragraph, im.caption) for im in before.images]
    new_images = [(im.image_id, im.after_paragraph, im.caption) for im in after.images]
    if old_images != new_images:
        problems.append("配图、位置或登记图注发生变化")
    if before.tags != after.tags:
        problems.append("标签发生变化")
    old_numbers = sorted(_NUMBER_TOKEN.findall(draft_to_json(before)))
    new_numbers = sorted(_NUMBER_TOKEN.findall(draft_to_json(after)))
    if old_numbers != new_numbers:
        problems.append("数字或规格标记发生变化")
    return problems


def edit_coherence(
    llm, prompts: Prompts, canonical_brief: str, draft: Draft, ev: Evidence
) -> tuple[Draft, list[str], list[str]]:
    candidate, refusals = parse_draft(llm.json(
        prompts.render("coherence", {
            "规范需求": canonical_brief,
            "已通过稿件": draft_to_json(draft),
            "事实点清单": facts_block(ev),
        }),
        system=prompts.writer_system,
        temperature=0.2,
    ))
    candidate = fill_captions(candidate, ev)
    violations = fact_lock_violations(draft, candidate)
    return (draft if violations else candidate), violations, refusals


def generate(
    llm, prompts: Prompts, brief: Brief, ev: Evidence, strategy: Strategy,
    context_package: ContextPackage | None = None,
) -> tuple[Draft, list[str]]:
    user = prompts.render(
        "compose",
        {
            "需求": brief_block(brief),
            "策略": strategy_block(strategy, context_package),
            "事实点清单": facts_block(ev, context_package),
            "可用图片": images_block(ev, brief.product, context_package),
            "表达参考": context_block(ev, context_package),
        },
    )
    return parse_draft(llm.json(user, system=prompts.writer_system, temperature=0.7))


def rewrite(
    llm,
    prompts: Prompts,
    draft: Draft,
    issues_text: str,
    brief: Brief,
    ev: Evidence,
    strategy: Strategy,
    context_package: ContextPackage | None = None,
) -> tuple[Draft, list[str]]:
    """按核验与合规发现的问题改写:只动有问题的部分。"""
    user = prompts.render(
        "rewrite",
        {
            "稿件": draft_to_json(draft),
            "问题": issues_text,
            "策略": strategy_block(strategy, context_package),
            "事实点清单": facts_block(ev, context_package),
            "可用图片": images_block(ev, brief.product, context_package),
            "表达参考": context_block(ev, context_package),
        },
    )
    return parse_draft(llm.json(user, system=prompts.writer_system, temperature=0.4))
