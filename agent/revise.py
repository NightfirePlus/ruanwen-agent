"""阶段7:确认环节的修改路由。

LLM 只负责理解用户这句话想改什么(判断意图);用哪个动作重跑、哪些结果可以复用,由规则决定:
  confirm      确认定稿
  rewrite      只是认为文案不行/临时要求 → 回到生成(复用需求、资料、策略)
  change_scene 换场景或人群 → 重新匹配策略表后生成(资料复用)
  change_platform 换平台 → 重新匹配策略表后生成(资料复用)
  change_product  换产品 → 从结构化需求重来(型号、资料全部作废)
不明确的修改意见不猜,交回去追问。
"""
from __future__ import annotations

import json
from dataclasses import fields

from . import config
from .models import Brief, Draft
from .prompts import Prompts

ACTIONS = ("confirm", "rewrite", "change_scene", "change_platform", "change_product", "unclear")


def classify(llm, prompts: Prompts, brief: Brief, draft: Draft, user_text: str) -> dict:
    """返回 {"action":..., "value":..., "note":...}。action 不在已知范围内一律按 unclear 处理。"""
    summary = f"标题:{draft.title}\n导语:{draft.intro}\n" + "\n".join(f"第{i}段:{p.text}" for i, p in enumerate(draft.body, 1))
    data = llm.json(
        prompts.render(
            "revise_intent",
            {
                "需求": json.dumps({f.name: getattr(brief, f.name) for f in fields(Brief)}, ensure_ascii=False),
                "稿件摘要": summary,
                "用户修改意见": user_text,
            },
        )
    )
    action = data.get("action") if data.get("action") in ACTIONS else "unclear"
    return {"action": action, "value": str(data.get("value") or "").strip(), "note": str(data.get("note") or "").strip()}


def apply(brief: Brief, intent: dict, user_text: str) -> tuple[Brief | None, str]:
    """把意图落到需求上。返回 (新需求, 下一步)。新需求为 None 表示无法执行,需要追问。

    下一步取值:done / generate / strategy / intake
    """
    action, value = intent["action"], intent["value"]
    new = Brief(**{f.name: getattr(brief, f.name) for f in fields(Brief)})
    if action == "confirm":
        return new, "done"
    if action == "rewrite":
        new.extra = (new.extra + ";" if new.extra else "") + (intent["note"] or user_text)
        return new, "generate"
    if action == "change_platform":
        target = config.match_alias(value, config.platforms())
        if not target:
            return None, "intake"
        new.platform = target
        return new, "strategy"
    if action == "change_scene":
        target = config.match_alias(value, config.scenes())
        new.scene = target or value
        if not new.scene:
            return None, "intake"
        return new, "strategy"
    if action == "change_product":
        if not value:
            return None, "intake"
        # 换产品:型号及其相关判断全部作废,其余需求保留,回到结构化环节重新抽取与校验
        new.product = value
        new.product_type = ""
        new.extra = ""
        return new, "intake"
    return None, "intake"
