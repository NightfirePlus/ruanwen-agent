"""把已确认的结构化需求转换成稳定、无额外推断的规范任务说明。"""
from __future__ import annotations

from .models import Brief


FIELDS = (
    ("产品", "product"),
    ("产品类型", "product_type"),
    ("目标受众", "audience"),
    ("使用场景", "scene"),
    ("发布平台", "platform"),
    ("创作目标", "goal"),
    ("购买阶段", "stage"),
    ("核心问题", "main_problem"),
    ("补充要求", "extra"),
)


def canonicalize(brief: Brief) -> str:
    """确定性格式化，不调用模型，也不添加用户未确认的信息。"""
    parts = [f"{label}：{value.strip()}" for label, name in FIELDS
             if (value := str(getattr(brief, name, "") or "")).strip()]
    return "；".join(parts) + "。"
