"""任务接收:检查四项必填信息,缺项则追问,不往下走。纯规则,不调用大模型。"""
from __future__ import annotations

import re

from . import config
from .models import Task

# 过于笼统、无法定位到具体型号的说法
VAGUE_PRODUCTS = {"轻薄本", "笔记本", "电脑", "华为电脑", "华为笔记本", "华为pc", "pc", "办公本", "游戏本"}
MODEL_HINT = re.compile(r"(MateBook|MateStation|擎云|MatePad|\b[A-Z]{1,3}\s?\d{1,2}\b)", re.I)


def check(task: Task) -> list[str]:
    """返回需要追问的问题;空列表表示信息已齐全。"""
    questions: list[str] = []

    product = task.product.strip()
    if not product:
        questions.append("这次要推广哪一款产品?请给出具体型号,例如 MateBook 14。")
    elif product.lower() in VAGUE_PRODUCTS or not MODEL_HINT.search(product):
        questions.append(
            f"“{product}”还不够具体。请告诉我具体型号,或者说明预算与使用需求,我再帮你确定推广机型。"
        )

    if not task.audience.strip():
        questions.append("面向哪类人群?例如大学新生、商务出差人士、设计创作者、家庭用户。")

    if not task.scene.strip():
        questions.append("属于哪种业务场景?例如开学季、职场办公、内容创作、家庭换新、门店到访、新品首发。")

    if not task.platform.strip():
        questions.append("发布在哪个平台?公众号、小红书、门店朋友圈或门店海报。")
    elif config.match_alias(task.platform, config.platforms()) is None:
        names = "、".join(config.platforms())
        questions.append(f"暂不支持平台“{task.platform}”。当前支持:{names}。")

    return questions


def normalize(task: Task) -> Task:
    """把平台、场景写成配置里的规范名;匹配不到的场景保留原文(按自定义场景处理)。"""
    platform = config.match_alias(task.platform, config.platforms()) or task.platform
    scene = config.match_alias(task.scene, config.scenes()) or task.scene
    return Task(
        product=task.product.strip(),
        audience=task.audience.strip(),
        scene=scene.strip(),
        platform=platform.strip(),
        extra=task.extra.strip(),
    )
