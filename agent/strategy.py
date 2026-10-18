"""阶段3:按场景判断匹配策略表。纯配置查表,不调用大模型。"""
from __future__ import annotations

from . import config
from .models import Brief, Strategy

PLATFORM_KEYS = ("form", "title", "length", "structure", "tone", "images", "cta", "tags", "field_mapping")


def match(brief: Brief) -> Strategy:
    """场景表和平台表合成一份策略。自定义场景(表里没有)只带空的场景策略,由提示词和 LLM 判断兜底。"""
    scene_cfg = config.scenes().get(brief.scene) or {}
    platform_cfg = config.platforms()[brief.platform]
    return Strategy(
        scene={
            "name": brief.scene,
            "angle": scene_cfg.get("angle", ""),
            "focus": scene_cfg.get("focus", []),
            "avoid": scene_cfg.get("avoid", []),
            "in_table": bool(scene_cfg),
        },
        platform={"name": brief.platform, **{k: platform_cfg[k] for k in PLATFORM_KEYS}},
    )
