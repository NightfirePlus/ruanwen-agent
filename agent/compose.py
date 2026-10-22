"""阶段4:综合需求、策略、事实点与图片,生成图文软文。提示词见 prompts/compose.md、writer_system.md。"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import fields

from .models import Brief, ContentBlock, ContentPlan, Draft, Evidence, ImagePlacement, Paragraph, Strategy
from .norm import usable_images
from .prompts import Prompts


def facts_block(ev: Evidence) -> str:
    return "\n".join(f"[{f.id}] ({f.model}) {f.text}  〔来源:{f.source_path}〕" for f in ev.facts)


def images_block(ev: Evidence, model: str) -> str:
    imgs = usable_images(ev, model)
    return "\n".join(json.dumps({"id": i.id, "model": i.model, "name": i.name,
        "source": i.source_path, "registered_caption": i.caption, "usage": i.usage,
        "note": i.note, "basis": "知识库已登记并绑定型号；未执行视觉模型审图"},
        ensure_ascii=False) for i in imgs) or "(无)"


def context_block(ev: Evidence) -> str:
    return json.dumps(ev.context, ensure_ascii=False)


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


def _chunk_values(
    canonical_brief: str,
    strategy: Strategy,
    plan: ContentPlan,
    block: ContentBlock,
    passed: list[ContentBlock],
    ev: Evidence,
) -> dict[str, str]:
    return {
        "规范需求": canonical_brief,
        "策略": json.dumps(
            {"scene": strategy.scene, "platform": strategy.platform,
             "performance_reference": strategy.performance},
            ensure_ascii=False,
        ),
        "内容计划": plan_to_json(plan),
        "当前块": json.dumps({
            "id": block.id, "kind": block.kind, "objective": block.objective,
            "fact_ids": block.fact_ids, "depends_on": block.depends_on,
            "version": block.version,
        }, ensure_ascii=False),
        "已通过块": json.dumps([
            {"id": item.id, "kind": item.kind,
             "draft": json.loads(draft_to_json(item.draft)) if item.draft else {}}
            for item in passed
        ], ensure_ascii=False),
        "事实点清单": facts_block(ev),
        "可用图片": images_block(ev, ev.facts[0].model if ev.facts else ""),
        "表达参考": context_block(ev),
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
) -> tuple[Draft, list[str]]:
    values = _chunk_values(canonical_brief, strategy, plan, block, passed, ev)
    values["可用图片"] = images_block(ev, brief.product)
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
) -> tuple[Draft, list[str]]:
    values = _chunk_values(canonical_brief, strategy, plan, block, passed, ev)
    values.update({
        "可用图片": images_block(ev, brief.product),
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


def generate(llm, prompts: Prompts, brief: Brief, ev: Evidence, strategy: Strategy) -> tuple[Draft, list[str]]:
    user = prompts.render(
        "compose",
        {
            "需求": brief_block(brief),
            "策略": json.dumps(
                {"scene": strategy.scene, "platform": strategy.platform,
                 "performance_reference": strategy.performance},
                ensure_ascii=False,
            ),
            "事实点清单": facts_block(ev),
            "可用图片": images_block(ev, brief.product),
            "表达参考": context_block(ev),
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
) -> tuple[Draft, list[str]]:
    """按核验与合规发现的问题改写:只动有问题的部分。"""
    user = prompts.render(
        "rewrite",
        {
            "稿件": draft_to_json(draft),
            "问题": issues_text,
            "策略": json.dumps(
                {"scene": strategy.scene, "platform": strategy.platform,
                 "performance_reference": strategy.performance},
                ensure_ascii=False,
            ),
            "事实点清单": facts_block(ev),
            "可用图片": images_block(ev, brief.product),
            "表达参考": context_block(ev),
        },
    )
    return parse_draft(llm.json(user, system=prompts.writer_system, temperature=0.4))
