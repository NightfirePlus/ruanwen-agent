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
    return "\n".join(f"[{i.id}] 型号:{i.model} 名称:{i.name} 来源:{i.source_path}" for i in imgs) or "(无)"


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
    """模型没写图片说明时,回退用图片索引里登记的官网图片说明。"""
    registered = {i.id: i.caption for i in ev.images if i.caption}
    for pl in draft.images:
        if not pl.caption.strip() and pl.image_id in registered:
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
        },
    )
    return parse_draft(llm.json(user, system=prompts.writer_system, temperature=0.7))


def rewrite(llm, prompts: Prompts, draft: Draft, issues_text: str) -> tuple[Draft, list[str]]:
    """按核验与合规发现的问题改写:只动有问题的部分。"""
    user = prompts.render("rewrite", {"稿件": draft_to_json(draft), "问题": issues_text})
    return parse_draft(llm.json(user, system=prompts.writer_system, temperature=0.4))
