"""阶段4:综合需求、策略、事实点与图片,生成图文软文。提示词见 prompts/compose.md、writer_system.md。"""
from __future__ import annotations

import json
from dataclasses import fields

from .models import Brief, Draft, Evidence, ImagePlacement, Paragraph, Strategy
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


def generate(llm, prompts: Prompts, brief: Brief, ev: Evidence, strategy: Strategy) -> tuple[Draft, list[str]]:
    user = prompts.render(
        "compose",
        {
            "需求": brief_block(brief),
            "策略": json.dumps({"scene": strategy.scene, "platform": strategy.platform}, ensure_ascii=False),
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
            "策略": json.dumps({"scene": strategy.scene, "platform": strategy.platform}, ensure_ascii=False),
            "事实点清单": facts_block(ev),
            "可用图片": images_block(ev, brief.product),
            "表达参考": context_block(ev),
        },
    )
    return parse_draft(llm.json(user, system=prompts.writer_system, temperature=0.4))
