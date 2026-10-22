"""按手册规定的四段格式导出:场景判断 / 知识库依据 / 图文软文 / 核验结果。"""
from __future__ import annotations

from .models import Session
from .norm import usable_images


_CHINESE_NUMERALS = "一二三四五六七八九十"


def image_label(index: int) -> str:
    """返回面向读者的配图序号，不暴露知识库图片 ID。"""
    if 1 <= index <= len(_CHINESE_NUMERALS):
        return f"图{_CHINESE_NUMERALS[index - 1]}"
    return f"图{index}"


def numbered_image_placements(draft):
    """按最终阅读顺序为图片编号。"""
    ordered = sorted(
        enumerate(draft.images), key=lambda item: (item[1].after_paragraph, item[0])
    )
    return [(placement, image_label(index)) for index, (_, placement) in enumerate(ordered, 1)]


def to_text(s: Session) -> str:
    """仅导出可发布文案，不包含检索依据和运行记录。"""
    d = s.draft
    if not d:
        return ""
    parts = [d.title, d.intro, *(p.text for p in d.body), d.cta]
    if d.tags:
        parts.append(" ".join(f"#{t.lstrip('#')}" for t in d.tags))
    return "\n\n".join(p for p in parts if p.strip()) + "\n"


def to_article_markdown(
    s: Session, image_url=None, include_heading: bool = False, include_image_source: bool = False
) -> str:
    """只渲染可发布内容；配图下方仅显示序号与必要说明。"""
    b, ev, d = s.brief, s.evidence, s.draft
    if not d:
        return ""
    lines = ["# 三、图文软文"] if include_heading else []
    lines.append(f"## {d.title}")
    if d.intro:
        lines += ["", d.intro]
    imgs = {i.id: i for i in usable_images(ev, b.product)} if ev else {}
    placed: dict[int, list] = {}
    for placement, label in numbered_image_placements(d):
        placed.setdefault(placement.after_paragraph, []).append((placement, label))

    def pics(position: int) -> list[str]:
        output = []
        for placement, label in placed.get(position, []):
            image = imgs.get(placement.image_id)
            if image is None:
                continue
            url = image_url(image) if image_url else image.origin_url
            if url and (image_url or url.startswith(("https://", "http://"))):
                output += ["", f"![{label}](<{url}>)", ""]
            caption = (placement.caption or "").strip()
            output += [f"> {label}{f'｜{caption}' if caption else ''}", ""]
        return output

    lines += pics(0)
    for index, paragraph in enumerate(d.body, 1):
        lines += ["", paragraph.text] + pics(index)
    if d.cta:
        lines += ["", d.cta]
    if d.tags:
        lines += ["", " ".join(f"#{tag.lstrip('#')}" for tag in d.tags)]
    return "\n".join(lines).strip() + "\n"


def to_markdown(s: Session, image_url=None) -> str:
    b, ev, d = s.brief, s.evidence, s.draft
    lines = [
        "# 一、场景判断",
        f"- 目标人群 / 使用场景:{b.audience} / {b.scene}",
        f"- 用户问题 / 内容目标 / 平台:{b.main_problem} / {b.goal} / {b.platform}",
        f"- 购买阶段:{b.stage}",
        f"- 内容切入点:{s.strategy.scene.get('angle', '') if s.strategy else ''}",
        "",
        "# 二、知识库依据",
    ]
    if ev and d:
        used = {x for p in d.body for x in p.fact_ids}
        for f in ev.facts:
            if f.id in used:
                lines.append(f"- {b.product} | {f.text} | 资料:{f.source_title}({f.source_path})")
        lines.append(f"- 相近型号(已防混淆):{'、'.join(ev.candidates) or '无'}")

    lines += ["", to_article_markdown(s, image_url=image_url, include_heading=True).rstrip()]

    lines += ["", "# 四、核验结果"]
    r = s.review
    if r:
        result = "阻断项已通过，仍有提示待确认" if r.passed and r.issues else "全部通过"
        if not r.passed:
            result = f"存在 {len(r.errors)} 项未解决问题"
        lines.append(f"- 产品事实 / 图片型号 / 合规:{result}")
        for i in r.issues:
            lines.append(f"  - [{_label(i)}] {i.where}:{i.detail}")
        if r.removed:
            lines.append("- 已自动删除的内容(无依据或不合规,已从稿件中移除):")
            for x in r.removed:
                text = f"“{x['text'][:40]}…”" if x["text"] else ""
                lines.append(f"  - {x['where']} {text} 原因:{x['reason']}")
        warns = [i for i in r.issues if i.severity == "warn"]
        lines.append(f"- 待确认信息:{'共 %d 项提示,见上方提示条目' % len(warns) if warns else '无'}")
    if ev and ev.dropped:
        lines.append(f"- 已丢弃的无依据摘录:{len(ev.dropped)} 条")
    return "\n".join(lines) + "\n"


def _label(i) -> str:
    kind = "事实" if i.stage == "fact" else "合规"
    src = "规则" if i.source == "rule" else "LLM"
    sev = "必须处理" if i.severity == "error" else "提示"
    return f"{kind}/{src}/{sev}"
