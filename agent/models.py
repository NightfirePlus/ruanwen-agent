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
class ContextPackage:
    """统一写作上下文契约。

    事实、图片、表达参考、长期偏好和历史效果各自保留独立权限，避免应用层
    把偏好文字拼进用户需求后被下游误当成事实。
    """

    contract_version: str = "writing-context-v1"
    canonical_brief: str = ""
    facts: list[Fact] = field(default_factory=list)
    images: list[ImageRef] = field(default_factory=list)
    expression_references: list[dict] = field(default_factory=list)
    user_preferences: dict[str, Any] = field(default_factory=dict)
    performance_references: list[dict] = field(default_factory=list)
    scene: dict[str, Any] = field(default_factory=dict)
    platform: dict[str, Any] = field(default_factory=dict)
    source_manifest: list[dict] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)
    authority: dict[str, str] = field(default_factory=lambda: {
        "facts": "唯一可用于产品参数、功能、条件和效果主张的证据",
        "images": "仅证明素材登记与型号归属，不证明画面中的产品事实",
        "expression_references": "仅影响角度、结构和措辞，不得升级为产品事实",
        "user_preferences": "仅影响语气、选题偏好和禁区，不得升级为产品事实",
        "performance_references": "仅影响角度、结构和行动引导，不得升级为产品事实",
    })

    def strategy_payload(self) -> dict[str, Any]:
        return {
            "scene": self.scene,
            "platform": self.platform,
            "user_preferences": self.user_preferences,
            "performance_reference": {
                "mode": "advisory_weight",
                "references": self.performance_references,
            },
            "authority": self.authority,
        }


@dataclass
class ContentBlock:
    """文章规划中的一个可独立生成、审核和重写的内容块。"""

    id: str
    kind: str                     # opening / body / closing
    objective: str
    fact_ids: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    status: str = "pending_generate"
    version: int = 0
    attempts: int = 0
    draft: Draft | None = None
    issues: list[Issue] = field(default_factory=list)
    removed: list[dict] = field(default_factory=list)


@dataclass
class ContentPlan:
    """文章分块计划；块顺序同时也是最终合并顺序。"""

    blocks: list[ContentBlock] = field(default_factory=list)


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
    canonical_brief: str = ""                         # 结构化需求的确定性自然语言表达
    evidence: Evidence | None = None
    strategy: Strategy | None = None
    context_package: ContextPackage | None = None
    content_plan: ContentPlan | None = None
    draft: Draft | None = None
    review: Review | None = None
    history: list[dict] = field(default_factory=list)     # 追问对话
    trace: list[dict] = field(default_factory=list)       # 每个阶段的记录

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict | None) -> "Session":
        """从持久化检查点恢复会话；未知字段会被忽略以兼容后续版本。"""
        data = value or {}

        def build(kind, raw):
            if raw is None:
                return None
            allowed = kind.__dataclass_fields__
            return kind(**{key: item for key, item in raw.items() if key in allowed})

        def draft(raw):
            if raw is None:
                return None
            return Draft(
                title=raw.get("title", ""),
                intro=raw.get("intro", ""),
                body=[build(Paragraph, row) for row in raw.get("body", [])],
                cta=raw.get("cta", ""),
                tags=list(raw.get("tags", [])),
                images=[build(ImagePlacement, row) for row in raw.get("images", [])],
            )

        evidence_raw = data.get("evidence")
        evidence = None
        if evidence_raw is not None:
            evidence = Evidence(
                facts=[build(Fact, row) for row in evidence_raw.get("facts", [])],
                images=[build(ImageRef, row) for row in evidence_raw.get("images", [])],
                candidates=list(evidence_raw.get("candidates", [])),
                conflicts=list(evidence_raw.get("conflicts", [])),
                dropped=list(evidence_raw.get("dropped", [])),
                sources=list(evidence_raw.get("sources", [])),
                context=list(evidence_raw.get("context", [])),
                originals=list(evidence_raw.get("originals", [])),
                review_stats=dict(evidence_raw.get("review_stats", {})),
            )

        plan_raw = data.get("content_plan")
        plan = None
        if plan_raw is not None:
            blocks = []
            for raw in plan_raw.get("blocks", []):
                blocks.append(ContentBlock(
                    id=raw.get("id", ""), kind=raw.get("kind", "body"),
                    objective=raw.get("objective", ""),
                    fact_ids=list(raw.get("fact_ids", [])),
                    depends_on=list(raw.get("depends_on", [])),
                    status=raw.get("status", "pending_generate"),
                    version=int(raw.get("version", 0)),
                    attempts=int(raw.get("attempts", 0)),
                    draft=draft(raw.get("draft")),
                    issues=[build(Issue, row) for row in raw.get("issues", [])],
                    removed=list(raw.get("removed", [])),
                ))
            plan = ContentPlan(blocks)

        review_raw = data.get("review")
        review = None
        if review_raw is not None:
            review = Review(
                issues=[build(Issue, row) for row in review_raw.get("issues", [])],
                removed=list(review_raw.get("removed", [])),
            )
        strategy_raw = data.get("strategy")
        strategy = build(Strategy, strategy_raw) if strategy_raw is not None else None
        context_raw = data.get("context_package")
        context_package = None
        if context_raw is not None:
            context_package = ContextPackage(
                contract_version=context_raw.get("contract_version", "writing-context-v1"),
                canonical_brief=context_raw.get("canonical_brief", ""),
                facts=[build(Fact, row) for row in context_raw.get("facts", [])],
                images=[build(ImageRef, row) for row in context_raw.get("images", [])],
                expression_references=list(context_raw.get("expression_references", [])),
                user_preferences=dict(context_raw.get("user_preferences", {})),
                performance_references=list(context_raw.get("performance_references", [])),
                scene=dict(context_raw.get("scene", {})),
                platform=dict(context_raw.get("platform", {})),
                source_manifest=list(context_raw.get("source_manifest", [])),
                verification=dict(context_raw.get("verification", {})),
                authority=dict(context_raw.get("authority", {})) or ContextPackage().authority,
            )
        return cls(
            brief=build(Brief, data.get("brief") or {}),
            canonical_brief=data.get("canonical_brief", ""),
            evidence=evidence,
            strategy=strategy,
            context_package=context_package,
            content_plan=plan,
            draft=draft(data.get("draft")),
            review=review,
            history=list(data.get("history", [])),
            trace=list(data.get("trace", [])),
        )

    def prepare_resume(self) -> None:
        """把进程中断时遗留的瞬时队列状态改回可重新调度状态。"""
        if not self.content_plan:
            return
        for block in self.content_plan.blocks:
            if block.status in ("generating", "reviewing"):
                block.status = "pending_generate"
