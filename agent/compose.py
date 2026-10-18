"""图文生成与修订。提示词见 prompts/compose.md 与 prompts/revise.md,这里只负责填充变量和解析输出。"""
from __future__ import annotations

import json

from .models import Draft, Fact, ImagePlacement, ImageRef, Paragraph, Task
from .prompts import Prompts


def _facts_block(facts: list[Fact]) -> str:
    return "\n".join(f"[{f.id}] ({f.model}) {f.text}" for f in facts)


def _images_block(images: list[ImageRef]) -> str:
    return "\n".join(f"[{i.id}] 型号:{i.model} 名称:{i.name} 来源:{i.source}" for i in images) or "(无)"


def _compose_user(prompts: Prompts, task: Task, judgement: dict, strategy: dict, facts, images) -> str:
    return prompts.render(
        "compose",
        {
            "任务": (
                f"产品:{task.product};人群:{task.audience};场景:{task.scene};"
                f"平台:{task.platform};临时要求:{task.extra or '无'}"
            ),
            "场景判断": json.dumps(judgement, ensure_ascii=False),
            "内容策略": (
                f"切入点:{strategy['angle']};卖点优先顺序:{'、'.join(strategy['focus_order'])};"
                f"本场景需避免:{'、'.join(strategy['avoid'])}"
            ),
            "平台规则": json.dumps(strategy["platform_rules"], ensure_ascii=False),
            "事实点清单": _facts_block(facts),
            "可用图片": _images_block(images),
        },
    )


def parse_draft(data: dict) -> tuple[Draft, list[str]]:
    body = []
    for p in data.get("body", []):
        if isinstance(p, dict):
            body.append(Paragraph(text=str(p.get("text", "")).strip(), fact_ids=list(p.get("fact_ids") or [])))
        else:
            body.append(Paragraph(text=str(p).strip()))
    images = []
    for im in data.get("images", []):
        try:
            images.append(
                ImagePlacement(
                    image_id=str(im["image_id"]),
                    after_paragraph=int(im.get("after_paragraph", 0)),
                    caption=str(im.get("caption", "")),
                    reason=str(im.get("reason", "")),
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


def generate(llm, prompts: Prompts, task: Task, judgement: dict, strategy: dict, facts, images) -> tuple[Draft, list[str]]:
    user = _compose_user(prompts, task, judgement, strategy, facts, images)
    return parse_draft(llm.chat_json(prompts.writer_system, user, temperature=0.7))


def revise(
    llm, prompts: Prompts, task, judgement, strategy, facts, images, draft: Draft, issues_text: str
) -> tuple[Draft, list[str]]:
    """根据核验问题修订:只改有问题的部分,保持其余内容。"""
    current = json.dumps(
        {
            "title": draft.title,
            "intro": draft.intro,
            "body": [{"text": p.text, "fact_ids": p.fact_ids} for p in draft.body],
            "cta": draft.cta,
            "tags": draft.tags,
            "images": [im.__dict__ for im in draft.images],
        },
        ensure_ascii=False,
    )
    user = (
        _compose_user(prompts, task, judgement, strategy, facts, images)
        + "\n\n"
        + prompts.render("revise", {"当前稿件": current, "核验问题": issues_text})
    )
    return parse_draft(llm.chat_json(prompts.writer_system, user, temperature=0.4))
