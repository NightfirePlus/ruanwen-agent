"""按手册规定的四段格式导出。核验已通过的稿件才会剥离事实编号。"""
from __future__ import annotations

from .models import Result


def to_markdown(r: Result) -> str:
    t = r.task
    j = r.scene_judgement
    lines = ["# 一、场景判断"]
    lines += [
        f"- 目标人群 / 使用场景:{j.get('audience', t.audience)} / {j.get('usage_scene', t.scene)}",
        f"- 用户问题 / 内容目标 / 平台:{j.get('main_problem', '')} / {j.get('goal', '')} / {t.platform}",
        f"- 内容切入点:{r.strategy.get('angle', '')}",
        "",
        "# 二、知识库依据",
    ]
    used = {x for p in (r.draft.body if r.draft else []) for x in p.fact_ids}
    for f in r.facts:
        if f.id in used:
            lines.append(f"- [{f.id}] {f.model} | {f.text} | 资料:{f.source_title}")
    lines += ["", "# 三、图文软文"]
    if r.draft:
        d = r.draft
        lines.append(f"## {d.title}")
        if d.intro:
            lines += ["", d.intro]
        placed: dict[int, list] = {}
        for pl in d.images:
            placed.setdefault(pl.after_paragraph, []).append(pl)
        img_by_id = {i.id: i for i in r.images}

        def img_lines(pos: int) -> list[str]:
            out = []
            for pl in placed.get(pos, []):
                im = img_by_id.get(pl.image_id)
                if im:
                    out.append(f"> 【配图 {im.id}】{im.name}｜来源:{im.source}｜说明:{pl.caption}")
            return out

        lines += img_lines(0)
        for i, p in enumerate(d.body, 1):
            lines += ["", p.text] + img_lines(i)
        if d.cta:
            lines += ["", d.cta]
        if d.tags:
            lines += ["", " ".join(f"#{x.lstrip('#')}" for x in d.tags)]
    lines += ["", "# 四、核验结果"]
    errors = [i for i in r.issues if i.severity == "error"]
    warns = [i for i in r.issues if i.severity == "warn"]
    lines.append(f"- 产品事实 / 图片型号 / 合规:{'全部通过' if not errors else f'存在 {len(errors)} 项未解决问题'}")
    for i in r.issues:
        mark = "已处理" if i.action and i.severity == "error" and not errors else i.severity
        lines.append(f"  - [{i.kind}/{mark}] {i.where} {i.detail}" + (f"(处理:{i.action})" if i.action else ""))
    for x in r.refusals:
        lines.append(f"- 已拒绝写入:{x}")
    lines.append(f"- 待确认信息:{'无' if not warns else '、'.join(w.detail for w in warns)}")
    return "\n".join(lines) + "\n"
