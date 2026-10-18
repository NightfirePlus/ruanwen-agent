"""场景解析与策略匹配。场景解析用大模型;策略由配置表合成,不调用大模型。"""
from __future__ import annotations

from . import config
from .models import Task

SCENE_PROMPT = """你要为华为PC营销内容做场景判断。根据任务信息输出一个JSON对象,字段:
audience(目标人群,具体描述)、usage_scene(典型使用场景)、stage(购买阶段,取值:初步了解/对比选购/犹豫观望/需求明确)、
main_problem(用户最关心的核心问题)、goal(内容目标,取值:种草/转化到店/咨询引导/促销告知)、angle(本篇的内容切入点,一句话)。
只依据任务信息和场景参考做判断,不要引入任何产品参数。只输出JSON。"""


def parse(llm, task: Task) -> dict:
    scene_cfg = config.scenes().get(task.scene)
    ref = (
        f"场景参考:典型人群={scene_cfg['typical_audience']};建议切入={scene_cfg['angle']};重点={'、'.join(scene_cfg['focus'])}"
        if scene_cfg
        else "场景参考:这是自定义场景,请依据任务信息自行判断。"
    )
    user = (
        f"产品:{task.product}\n目标人群:{task.audience}\n业务场景:{task.scene}\n"
        f"发布平台:{task.platform}\n临时要求:{task.extra or '无'}\n{ref}"
    )
    return llm.chat_json(SCENE_PROMPT, user)


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
