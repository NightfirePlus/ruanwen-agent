"""数据结构。整条链路只靠这些结构传递信息。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Brief:
    """阶段1:结构化需求。含各项判断及其依据,字段齐全才算追问结束。"""

    product: str = ""              # 具体型号
    product_type: str = ""         # 产品类型,如 笔记本/平板/台式机;换类型要整体重来
    audience: str = ""             # 人群判断
    scene: str = ""                # 场景判断(规范名)
    platform: str = ""             # 平台判断(规范名)
    goal: str = ""                 # 目标判断:种草/转化到店/咨询引导/促销告知
    stage: str = ""                # 购买阶段
    main_problem: str = ""         # 用户最关心的问题
    extra: str = ""                # 临时要求
    llm_notes: str = ""            # LLM 的综合判断说明

    REQUIRED = ("product", "audience", "scene", "platform", "goal")

    def missing(self) -> list[str]:
        return [k for k in self.REQUIRED if not getattr(self, k).strip()]


@dataclass
class Fact:
    id: str                        # F1、F2……
    model: str                     # 事实所属型号
    text: str                      # 资料原文片段
    source_title: str              # 资料标题
    source_path: str = ""          # 资料在知识库中的路径
    media_id: str = ""


@dataclass
class ImageRef:
    id: str                        # I1、I2……
    model: str                     # 图片所属型号;无法确认时为空
    name: str
    media_id: str
    source_path: str = ""
    caption: str = ""              # 官网图片说明(来自图片索引)
    usage: str = ""                # 建议用途
    origin_url: str = ""           # 原始图片 URL
    note: str = ""                 # 型号未确认的原因
    review: dict = field(default_factory=dict)  # 兼容旧运行记录；当前不执行视觉模型审图


@dataclass
class Evidence:
    """阶段2的产出:匹配到的资料依据。"""

    facts: list[Fact] = field(default_factory=list)
    images: list[ImageRef] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)   # 相近型号,用于防混淆
    conflicts: list[str] = field(default_factory=list)    # 资料冲突说明
    dropped: list[str] = field(default_factory=list)      # 被丢弃的无依据摘录
    sources: list[str] = field(default_factory=list)      # 资料来源清单
    context: list[dict] = field(default_factory=list)  # 已审阅表达参考，不作为产品参数证据
    originals: list[dict] = field(default_factory=list)  # 核验所需原始材料及条件
    review_stats: dict = field(default_factory=dict)


@dataclass
class Strategy:
    """阶段3的产出:由场景表与平台表合成。"""

    scene: dict[str, Any] = field(default_factory=dict)
    platform: dict[str, Any] = field(default_factory=dict)
    performance: dict[str, Any] = field(default_factory=dict)  # 历史宣传效果的参考权重，不是事实证据


@dataclass
class Paragraph:
    text: str
    fact_ids: list[str] = field(default_factory=list)


@dataclass
class ImagePlacement:
    image_id: str
    after_paragraph: int           # 0 表示导语之后,N 表示第N段正文之后
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

    def segments(self) -> list[tuple[str, str]]:
        """按位置命名的文本片段,供核验与合规逐段检查。"""
        segs = [("标题", self.title), ("导语", self.intro)]
        segs += [(f"正文第{i}段", p.text) for i, p in enumerate(self.body, 1)]
        segs += [("行动引导", self.cta), ("标签", " ".join(self.tags))]
        segs += [(f"图注 {im.image_id}", im.caption) for im in self.images]
        return [(w, t) for w, t in segs if t]


@dataclass
class Issue:
    stage: str                     # fact / compliance
    source: str                    # rule(规则)/ llm
    severity: str                  # error 必须处理 / warn 提示
    where: str
    detail: str
    suggestion: str = ""
    ref: str = ""                  # 指向的具体对象,如图片编号;"count" 表示图片数量超限


@dataclass
class Review:
    """阶段5、6的合并结果。"""

    issues: list[Issue] = field(default_factory=list)
    removed: list[dict] = field(default_factory=list)   # 被自动删除的内容:{where, text, reason}

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def passed(self) -> bool:
        return not self.errors


@dataclass
class Session:
    """一次完整对话的状态,多轮追问与确认后的修改都在它上面进行。"""

    brief: Brief = field(default_factory=Brief)
    evidence: Evidence | None = None
    strategy: Strategy | None = None
    draft: Draft | None = None
    review: Review | None = None
    history: list[dict] = field(default_factory=list)     # 追问对话
    trace: list[dict] = field(default_factory=list)       # 每个阶段的记录

    def to_dict(self) -> dict:
        return asdict(self)
