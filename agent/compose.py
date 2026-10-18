"""图文生成:带事实编号的生成,以及按核验结果的修订。"""
from __future__ import annotations

import json

from . import config
from .models import Draft, Fact, ImagePlacement, ImageRef, Paragraph, Task

OUTPUT_SPEC = """只输出一个JSON对象:
{
 "title": "标题(按平台规则)",
 "intro": "导语/开头(按平台规则,不需要时为空字符串)",
 "body": [{"text": "一段正文", "fact_ids": ["F1","F3"]}],
 "cta": "行动引导",
 "tags": ["话题标签,平台不需要时为空数组"],
 "images": [{"image_id":"I2","after_paragraph":1,"caption":"图片说明","reason":"为什么放在这里"}],
 "refusals": ["对用户临时要求中无法满足部分的说明,没有则为空数组"]
}
规则:
- 凡写到产品型号、参数、功能、特点的段落,fact_ids 必须列出所依据的事实点编号;纯引导性语句 fact_ids 可为空数组。
- 正文中出现的数字、参数、功能名称必须与对应事实点原文一致。
- images 只能使用"可用图片"里的 image_id;数量遵守平台规则;after_paragraph 为 0 表示导语之后,N 表示第N段正文之后。
- 临时要求若涉及事实点中没有的价格、补贴、参数,不要写入,并在 refusals 中说明。
- 不得出现第一人称使用体验。"""


def _facts_block(facts: list[Fact]) -> str:
    return "\n".join(f"[{f.id}] ({f.model}) {f.text}" for f in facts)


def _images_block(images: list[ImageRef]) -> str:
    return "\n".join(f"[{i.id}] 型号:{i.model} 名称:{i.name} 来源:{i.source}" for i in images) or "(无)"


def _user_prompt(task: Task, judgement: dict, strategy: dict, facts, images) -> str:
    return (
        f"【任务】产品:{task.product};人群:{task.audience};场景:{task.scene};平台:{task.platform};"
        f"临时要求:{task.extra or '无'}\n"
        f"【场景判断】{json.dumps(judgement, ensure_ascii=False)}\n"
        f"【内容策略】切入点:{strategy['angle']};卖点优先顺序:{'、'.join(strategy['focus_order'])};"
        f"本场景需避免:{'、'.join(strategy['avoid'])}\n"
        f"【平台规则】{json.dumps(strategy['platform_rules'], ensure_ascii=False)}\n"
        f"【事实点清单】\n{_facts_block(facts)}\n"
        f"【可用图片】\n{_images_block(images)}\n\n{OUTPUT_SPEC}"
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


def generate(llm, task: Task, judgement: dict, strategy: dict, facts, images) -> tuple[Draft, list[str]]:
    data = llm.chat_json(config.system_prompt(), _user_prompt(task, judgement, strategy, facts, images), temperature=0.7)
    return parse_draft(data)


def revise(llm, task, judgement, strategy, facts, images, draft: Draft, issues_text: str) -> tuple[Draft, list[str]]:
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
        _user_prompt(task, judgement, strategy, facts, images)
        + f"\n\n【当前稿件】\n{current}\n\n【核验发现的问题】\n{issues_text}\n"
        "请修订稿件:删除或改写有问题的内容,其余保持不变,输出完整的JSON(格式同上)。"
    )
    data = llm.chat_json(config.system_prompt(), user, temperature=0.4)
    return parse_draft(data)
