"""自动删除:改写之后仍是 error 的内容,按位置删掉并留痕。

手册要求“无依据内容必须删除或标记”,而不是因为一句话有问题就整篇作废。
规则:
- 能定位到具体位置的才删:正文某段、导语、行动引导、标签、某张图片、超出平台数量的图片。
- 只要有一个 error 无法定位(例如标题有问题、LLM 没给出明确位置),就整体不动,交还调用方阻断,
  不在不确定的情况下乱删。
- 删除正文某段后,后面图片的插入位置同步前移。
"""
from __future__ import annotations

import copy
import re

from .models import Draft, Issue

BODY = re.compile(r"^正文第(\d+)段$")
TEXT_FIELDS = {"导语": "intro", "行动引导": "cta"}


def _why(reasons: list[str]) -> str:
    return ";".join(dict.fromkeys(reasons))


def apply(draft: Draft, errors: list[Issue], max_images: int) -> tuple[Draft, list[dict], list[Issue]]:
    """返回 (处理后的稿件, 被删除的内容, 无法定位的问题)。有无法定位的问题时稿件原样返回。"""
    d = copy.deepcopy(draft)
    reasons: dict[str, list[str]] = {}
    unresolved: list[Issue] = []
    drop_images: set[str] = set()
    trim = False

    for e in errors:
        w = e.where or ""
        m = BODY.match(w)
        if m and 1 <= int(m.group(1)) <= len(d.body):
            reasons.setdefault(w, []).append(e.detail)
        elif w in TEXT_FIELDS or w == "标签":
            reasons.setdefault(w, []).append(e.detail)
        elif w == "配图方案" and e.ref == "count":
            trim = True
            reasons.setdefault("配图数量", []).append(e.detail)
        elif w == "配图方案" and e.ref:
            drop_images.add(e.ref)
            reasons.setdefault(f"配图 {e.ref}", []).append(e.detail)
        else:
            unresolved.append(e)
    if unresolved:
        return draft, [], unresolved

    removed: list[dict] = []

    # 正文:从后往前删,下标不会错位;图片插入位置同步前移
    body_idx = sorted((int(BODY.match(w).group(1)) for w in reasons if BODY.match(w)), reverse=True)
    body_removed = []
    for n in body_idx:
        para = d.body.pop(n - 1)
        body_removed.append({"where": f"正文第{n}段", "text": para.text, "reason": _why(reasons[f"正文第{n}段"])})
        for pl in d.images:
            if pl.after_paragraph >= n:
                pl.after_paragraph -= 1
    removed += reversed(body_removed)

    for w, attr in TEXT_FIELDS.items():
        if w in reasons:
            removed.append({"where": w, "text": getattr(d, attr), "reason": _why(reasons[w])})
            setattr(d, attr, "")
    if "标签" in reasons:
        removed.append({"where": "标签", "text": " ".join(d.tags), "reason": _why(reasons["标签"])})
        d.tags = []

    if drop_images:
        keep = []
        for pl in d.images:
            if pl.image_id in drop_images:
                removed.append({"where": f"配图 {pl.image_id}", "text": pl.caption, "reason": _why(reasons[f"配图 {pl.image_id}"])})
            else:
                keep.append(pl)
        d.images = keep
    if trim and len(d.images) > max_images:
        for pl in d.images[max_images:]:
            removed.append({"where": f"配图 {pl.image_id}", "text": pl.caption, "reason": _why(reasons["配图数量"])})
        d.images = d.images[:max_images]
    return d, removed, []
