"""事实与图片核验。以代码规则为主:数字、型号、图片归属都由代码比对,不靠模型自评。"""
from __future__ import annotations

import re

from . import config
from .models import Draft, Fact, ImageRef, Issue
from .retrieve import norm

# 带单位的数值,如 1.36kg、16GB、14.2英寸、66W、3.1K、5G
NUM_UNIT = re.compile(
    r"\d+(?:\.\d+)?\s*(?:kg|KG|g|GB|TB|MB|Hz|W|Wh|mAh|mm|cm|英寸|寸|小时|h|分钟|nm|核|线程|万|K|k|%|倍|代|瓦|克|毫米|吋)",
)
# 型号写法,如 MateBook 14、MateBook X Pro、MateStation S
MODEL_TOKEN = re.compile(r"(?:MateBook|MateStation|擎云|MatePad)\s?(?:[A-Z]{1,3}\s?)?(?:Pro|Air|GT)?\s?\d{0,2}", re.I)


def _compact(s: str) -> str:
    return re.sub(r"\s+", "", s or "").lower()


def _nums(text: str) -> set[str]:
    return {_compact(m) for m in NUM_UNIT.findall(text)}


def check(draft: Draft, facts: list[Fact], images: list[ImageRef], task_product: str, page_limits: dict) -> list[Issue]:
    issues: list[Issue] = []
    fact_by_id = {f.id: f for f in facts}
    all_fact_text = _compact("".join(f.text for f in facts))
    main_model = norm(task_product)

    # 1. 正文逐段:引用的事实编号必须存在,且数值必须出现在所引事实中
    for i, p in enumerate(draft.body, 1):
        where = f"正文第{i}段"
        cited = [fact_by_id[x] for x in p.fact_ids if x in fact_by_id]
        missing = [x for x in p.fact_ids if x not in fact_by_id]
        if missing:
            issues.append(Issue("fact", "error", f"引用了不存在的事实点 {','.join(missing)}", where, "删除该引用或改写该段"))
        cited_text = _compact("".join(f.text for f in cited))
        for n in _nums(p.text):
            if n not in cited_text:
                level = "error" if n not in all_fact_text else "warn"
                detail = (
                    f"数值“{n}”不在知识库事实中" if level == "error" else f"数值“{n}”存在于其他事实点,但未被本段引用的事实支持"
                )
                issues.append(Issue("fact", level, detail, where, "删除或改写该表述"))
        # 产品信息段落却没有任何依据
        if not p.fact_ids and _nums(p.text):
            issues.append(Issue("fact", "error", "含数值的段落没有标注事实依据", where, "补充依据或删除"))

    # 2. 标题、导语、行动引导中的数值同样需要依据
    for where, text in (("标题", draft.title), ("导语", draft.intro), ("行动引导", draft.cta)):
        for n in _nums(text):
            if n not in all_fact_text:
                issues.append(Issue("fact", "error", f"数值“{n}”不在知识库事实中", where, "删除或改写"))

    # 3. 型号一致性:正文出现的其他型号不得与主推型号混淆
    cited_models = {f.model for p in draft.body for x in p.fact_ids if (f := fact_by_id.get(x))}
    for m in cited_models:
        if norm(m) != main_model:
            issues.append(Issue("model", "error", f"正文引用了型号“{m}”的事实,但主推型号是“{task_product}”", "正文", "改为主推型号的事实"))
    for where, text in (("标题", draft.title), ("导语", draft.intro)) + tuple((f"正文第{i}段", p.text) for i, p in enumerate(draft.body, 1)):
        for tok in MODEL_TOKEN.findall(text):
            t = norm(tok)
            if t and t not in main_model and main_model not in t and len(t) > 4:
                issues.append(Issue("model", "error", f"出现了非主推型号的写法“{tok.strip()}”", where, "核对型号"))

    # 4. 图片:必须在可用清单内、型号一致、数量符合平台、位置有效
    img_by_id = {im.id: im for im in images}
    for pl in draft.images:
        im = img_by_id.get(pl.image_id)
        if im is None:
            issues.append(Issue("image", "error", f"图片 {pl.image_id} 不在知识库图片清单中", "配图方案", "移除该图片"))
            continue
        if norm(im.model) != main_model:
            issues.append(Issue("image", "error", f"图片 {im.id}《{im.name}》型号为“{im.model}”,与主推型号不一致", "配图方案", "移除该图片"))
        if not (0 <= pl.after_paragraph <= len(draft.body)):
            issues.append(Issue("image", "warn", f"图片 {im.id} 的插入位置超出正文范围", "配图方案", "调整位置"))
        if not pl.caption.strip():
            issues.append(Issue("image", "warn", f"图片 {im.id} 缺少图片说明", "配图方案", "补充说明"))

    lo, hi = page_limits.get("min", 0), page_limits.get("max", 99)
    n = len(draft.images)
    if n > hi:
        issues.append(Issue("platform", "error", f"图片数量 {n} 超过平台上限 {hi}", "配图方案", "删减图片"))
    elif n < lo:
        issues.append(Issue("platform", "warn", f"图片数量 {n} 少于平台建议的 {lo} 张(可能是知识库可用图片不足)", "配图方案", ""))
    return issues


def detect_conflicts(facts: list[Fact]) -> list[str]:
    """同一型号下,同类参数出现不同取值时视为冲突。简单规则:同一单位下的多个不同数值。"""
    by_key: dict[tuple[str, str], set[str]] = {}
    for f in facts:
        for m in NUM_UNIT.findall(f.text):
            unit = re.sub(r"[\d.\s]", "", m)
            # 参数名取数值之前的文字(如“重量约”),避免数值本身混入分组键
            before = f.text.split(m)[0]
            label = re.sub(r"[约共达为:：,,\s]", "", re.split(r"[。;;,,]", before)[-1])[-4:]
            by_key.setdefault((norm(f.model), unit + label), set()).add(_compact(m))
    return [f"型号 {k[0]} 的“{k[1]}”出现多个取值:{'、'.join(sorted(v))}" for k, v in by_key.items() if len(v) > 1]
