"""各阶段共用的小工具,避免模块之间互相导入。"""
from __future__ import annotations

import re

from .models import Evidence, ImageRef


def norm_model(s: str) -> str:
    """型号归一化:小写,去空格、连字符、下划线。"""
    return re.sub(r"[\s\-_·]", "", (s or "").lower())


def mentioned_models(text: str, names: list[str]) -> set[str]:
    """文本里提到了哪些型号。按名称从长到短匹配,并在匹配后抹掉,
    这样 "MateBook 14s" 不会被误判成提到了 "MateBook 14"(后者是前者的前缀)。"""
    rest = norm_model(text)
    found: set[str] = set()
    for name in sorted(names, key=lambda n: len(norm_model(n)), reverse=True):
        key = norm_model(name)
        if key and key in rest:
            found.add(name)
            rest = rest.replace(key, "\0")
    return found


def usable_images(ev: Evidence, model: str) -> list[ImageRef]:
    """只有确认属于该型号的图片才可用;所属型号未确认的不进入生成。"""
    return [i for i in ev.images if i.model and norm_model(i.model) == norm_model(model)]
