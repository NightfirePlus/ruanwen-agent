"""按手册规定的四段格式导出:场景判断 / 知识库依据 / 图文软文 / 核验结果。"""
from __future__ import annotations

from .models import Session
from .norm import usable_images


def to_markdown(s: Session) -> str:
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

    lines += ["", "# 三、图文软文"]
    if d:
        lines.append(f"## {d.title}")
        if d.intro:
            lines += ["", d.intro]
        imgs = {i.id: i for i in ev.images} if ev else {}
        placed: dict[int, list] = {}
        for pl in d.images:
            placed.setdefault(pl.after_paragraph, []).append(pl)

        def pics(pos: int) -> list[str]:
            return [
                f"> 【配图 {imgs[pl.image_id].id}】{imgs[pl.image_id].name}｜来源:{imgs[pl.image_id].source_path}｜说明:{pl.caption}"
                for pl in placed.get(pos, [])
                if pl.image_id in imgs
            ]

        lines += pics(0)
        for i, p in enumerate(d.body, 1):
            lines += ["", p.text] + pics(i)
        if d.cta:
            lines += ["", d.cta]
        if d.tags:
            lines += ["", " ".join(f"#{t.lstrip('#')}" for t in d.tags)]

    lines += ["", "# 四、核验结果"]
    r = s.review
    if r:
        lines.append(f"- 产品事实 / 图片型号 / 合规:{'全部通过' if r.passed else f'存在 {len(r.errors)} 项未解决问题'}")
        for i in r.issues:
            lines.append(f"  - [{_label(i)}] {i.where}:{i.detail}")
        if r.removed:
            lines.append("- 已自动删除的内容(无依据或不合规,已从稿件中移除):")
            for x in r.removed:
                text = f"“{x['text'][:40]}…”" if x["text"] else ""
                lines.append(f"  - {x['where']} {text} 原因:{x['reason']}")
        warns = [i for i in r.issues if i.severity == "warn"]
        lines.append(f"- 待确认信息:{'共 %d 项提示,见上方 warn 条目' % len(warns) if warns else '无'}")
    if ev and ev.dropped:
        lines.append(f"- 已丢弃的无依据摘录:{len(ev.dropped)} 条")
    return "\n".join(lines) + "\n"


def _label(i) -> str:
    kind = "事实" if i.stage == "fact" else "合规"
    src = "规则" if i.source == "rule" else "LLM"
    sev = "必须处理" if i.severity == "error" else "提示"
    return f"{kind}/{src}/{sev}"
