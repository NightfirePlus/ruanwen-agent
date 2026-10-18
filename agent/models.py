"""各环节之间传递的数据结构。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Task:
    product: str = ""        # 产品型号,如 "MateBook 14"
    audience: str = ""       # 目标人群
    scene: str = ""          # 业务场景
    platform: str = ""       # 发布平台
    extra: str = ""          # 临时要求,如"强调续航""不要提价格"


@dataclass
class Fact:
    id: str                  # F1、F2……
    model: str               # 事实所属型号
    text: str                # 摘自资料的原话
    source_title: str        # 资料标题
    media_id: str = ""


@dataclass
class ImageRef:
    id: str                  # I1、I2……
    model: str               # 图片所属型号(由目录/标题判定)
    name: str                # 图片名称
    media_id: str
    source: str              # 来源路径或标题
    folder: str = ""


@dataclass
class Paragraph:
    text: str
    fact_ids: list[str] = field(default_factory=list)


@dataclass
class ImagePlacement:
    image_id: str
    after_paragraph: int     # 插在第几段之后,0 表示导语之后
    caption: str
    reason: str = ""


@dataclass
class Draft:
    title: str = ""
    intro: str = ""
    body: list[Paragraph] = field(default_factory=list)
    cta: str = ""
    tags: list[str] = field(default_factory=list)
    images: list[ImagePlacement] = field(default_factory=list)

    def all_text(self) -> str:
        parts = [self.title, self.intro, self.cta, " ".join(self.tags)]
        parts += [p.text for p in self.body]
        return "\n".join(x for x in parts if x)


@dataclass
class Issue:
    kind: str                # fact / image / model / compliance / platform
    severity: str            # error 必须处理 / warn 提示
    detail: str
    where: str = ""
    action: str = ""         # 已采取的处理,如"已删除该段"


@dataclass
class Result:
    status: str                              # done / need_more_info / blocked
    task: Task = field(default_factory=Task)
    questions: list[str] = field(default_factory=list)   # need_more_info 时的追问
    reason: str = ""                                      # blocked 时的原因
    scene_judgement: dict[str, Any] = field(default_factory=dict)
    strategy: dict[str, Any] = field(default_factory=dict)
    facts: list[Fact] = field(default_factory=list)
    images: list[ImageRef] = field(default_factory=list)
    draft: Draft | None = None
    issues: list[Issue] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)     # 拒绝写入的用户要求
    trace: list[dict] = field(default_factory=list)       # 每一步的输入输出摘要

    def to_dict(self) -> dict:
        return asdict(self)
