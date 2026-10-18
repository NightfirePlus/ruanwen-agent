"""场景解析与策略匹配。场景解析用大模型(提示词见 prompts/scene.md);策略由配置表合成,不调用大模型。"""
from __future__ import annotations

from . import config
from .models import Task
from .prompts import Prompts


def parse(llm, prompts: Prompts, task: Task) -> dict:
    scene_cfg = config.scenes().get(task.scene)
    ref = (
        f"场景参考:典型人群={scene_cfg['typical_audience']};建议切入={scene_cfg['angle']};重点={'、'.join(scene_cfg['focus'])}"
        if scene_cfg
        else "场景参考:这是自定义场景,请依据任务信息自行判断。"
    )
    user = prompts.render(
        "scene",
        {
            "产品": task.product,
            "人群": task.audience,
            "场景": task.scene,
            "平台": task.platform,
            "临时要求": task.extra or "无",
            "场景参考": ref,
        },
    )
    return llm.chat_json(prompts.system, user)


def build_strategy(task: Task, judgement: dict) -> dict:
    """把场景配置与平台规则合成一份可解释的内容策略。"""
    scene_cfg = config.scenes().get(task.scene) or {}
    platform_cfg = config.platforms()[task.platform]
    return {
        "scene": task.scene,
        "angle": judgement.get("angle") or scene_cfg.get("angle", ""),
        "focus_order": scene_cfg.get("focus", []),
        "avoid": scene_cfg.get("avoid", []),
        "platform": task.platform,
        "platform_rules": {
            k: platform_cfg[k]
            for k in ("form", "title", "length", "structure", "tone", "images", "cta", "tags", "field_mapping")
        },
    }
