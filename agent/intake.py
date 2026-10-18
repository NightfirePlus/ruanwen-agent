"""阶段1:多轮追问,直到信息完整,再转成结构化需求(Brief)。

分工:LLM 负责从自然语言里抽取并判断各项字段、生成追问;规则负责把场景和平台落到策略表的规范名,
并决定"是否完整"。是否完整只由规则判断,不信任 LLM 的自我宣告。
"""
from __future__ import annotations

import json
import re
from dataclasses import fields

from . import config
from .models import Brief
from .prompts import Prompts

# 过于笼统、无法定位到具体型号的说法
VAGUE = {"轻薄本", "笔记本", "电脑", "华为电脑", "华为笔记本", "华为pc", "pc", "办公本", "游戏本", "平板"}
MODEL_HINT = re.compile(r"(MateBook|MateStation|擎云|MatePad|\b[A-Z]{1,3}\s?\d{1,2}\b)", re.I)
QUESTIONS = {
    "product": "这次推广哪一款产品?请给出具体型号,例如 MateBook 14。",
    "audience": "面向哪类人群?例如大学新生、商务出差人士、设计创作者、家庭用户。",
    "scene": "属于哪种业务场景?例如 {scenes}。",
    "platform": "发布在哪个平台?{platforms}。",
    "goal": "这篇内容想达到什么目标?种草、转化到店、咨询引导,还是促销告知?",
}
GOALS = ("种草", "转化到店", "咨询引导", "促销告知")


def _clean_str(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def merge(brief: Brief, extracted: dict) -> Brief:
    """把 LLM 抽取的结果并入已有信息。只接受已知字段;空值不会覆盖已有值。"""
    known = {f.name for f in fields(Brief)}
    data = {k: getattr(brief, k) for k in known}
    for k, v in extracted.items():
        if k in known and _clean_str(v):
            data[k] = _clean_str(v)
    return Brief(**data)


def normalize(brief: Brief) -> Brief:
    """场景、平台落到策略表规范名;目标限定在四个取值内。匹配不到的保持原样,留给 validate 追问。"""
    b = Brief(**{f.name: getattr(brief, f.name) for f in fields(Brief)})
    b.platform = config.match_alias(b.platform, config.platforms()) or b.platform
    b.scene = config.match_alias(b.scene, config.scenes()) or b.scene
    for g in GOALS:
        if g in b.goal:
            b.goal = g
            break
    return b


def validate(brief: Brief) -> list[str]:
    """规则判断信息是否完整,返回需要追问的问题。空列表表示可以进入下一阶段。"""
    qs: list[str] = []
    p = brief.product.strip()
    if not p:
        qs.append(QUESTIONS["product"])
    elif p.lower() in VAGUE or not MODEL_HINT.search(p):
        qs.append(f"“{p}”还不够具体。请告诉我具体型号,或者说明预算与使用需求,我再帮你确定推广机型。")

    if not brief.audience.strip():
        qs.append(QUESTIONS["audience"])
    if not brief.scene.strip():
        qs.append(QUESTIONS["scene"].format(scenes="、".join(config.scenes())))
    if not brief.platform.strip():
        qs.append(QUESTIONS["platform"].format(platforms="、".join(config.platforms())))
    elif brief.platform not in config.platforms():
        qs.append(f"暂不支持平台“{brief.platform}”。当前支持:{'、'.join(config.platforms())}。")
    if brief.goal not in GOALS:
        qs.append(QUESTIONS["goal"])
    return qs


def extract(llm, prompts: Prompts, brief: Brief, history: list[dict]) -> Brief:
    """调用 LLM,从最新的对话里抽取并判断各字段(含购买阶段、核心问题、综合说明)。"""
    user = prompts.render(
        "intake",
        {
            "已有信息": json.dumps({f.name: getattr(brief, f.name) for f in fields(Brief)}, ensure_ascii=False),
            "对话记录": "\n".join(f"{m['role']}:{m['content']}" for m in history),
            "场景选项": "、".join(config.scenes()),
            "平台选项": "、".join(config.platforms()),
        },
    )
    return merge(brief, llm.json(user))


def next_step(llm, prompts: Prompts, brief: Brief, history: list[dict]) -> tuple[Brief, list[str]]:
    """处理一轮用户输入:抽取 → 规范化 → 规则校验。返回 (更新后的需求, 追问)。"""
    brief = normalize(extract(llm, prompts, brief, history))
    return brief, validate(brief)
