"""阶段5:事实核验 = 规则 + LLM。

规则:数值是否出现在所引事实中、引用的事实编号是否存在、型号是否与主推一致、
      图片是否在可用清单内且型号一致、图片数量是否符合平台。
LLM:判断规则看不出来的语义问题(把参数用在不适用的场景、夸大功能、把"支持"写成"标配"等)。
LLM 的结论只能新增问题,不能撤销规则发现的问题;提示词只让模型报告必须修正的问题,
程序仍会把每一项按 error 处理。
"""
from __future__ import annotations

import re

from .compose import draft_to_json, facts_block
from .models import Brief, Draft, Evidence, Issue, Strategy
from .norm import llm_issues, mentioned_models, norm_model, spec_versions, usable_images
from .prompts import Prompts, PromptError

NUM_UNIT = re.compile(
    r"\d+(?:\.\d+)?\s*(?:kg|g|GB|TB|MB|Hz|W|Wh|mAh|mm|cm|英寸|寸|小时|h|分钟|nm|核|线程|万|K|%|倍|代|瓦|克|毫米)",
    re.I,
)
MODEL_TOKEN = re.compile(r"(?:MateBook|MateStation|擎云|MatePad)\s?(?:[A-Z]{1,3}\s?)?(?:Pro|Air|GT)?\s?\d{0,2}", re.I)


def _c(s: str) -> str:
    return re.sub(r"\s+", "", s or "").lower()


def _nums(text: str) -> set[str]:
    return {_c(m) for m in NUM_UNIT.findall(text)}


def rule_check(draft: Draft, ev: Evidence, brief: Brief, strategy: Strategy) -> list[Issue]:
    issues: list[Issue] = []
    by_id = {f.id: f for f in ev.facts}
    all_text = _c("".join(f.text for f in ev.facts))
    main = norm_model(brief.product)

    def add(sev, where, detail, sug="", ref=""):
        issues.append(Issue("fact", "rule", sev, where, detail, sug, ref))

    for i, p in enumerate(draft.body, 1):
        where = f"正文第{i}段"
        missing = [x for x in p.fact_ids if x not in by_id]
        if missing:
            add("error", where, f"引用了不存在的事实点 {','.join(missing)}", "删除该引用或改写该段")
        cited = _c("".join(by_id[x].text for x in p.fact_ids if x in by_id))
        for n in _nums(p.text):
            if n not in cited:
                if n not in all_text:
                    add("error", where, f"数值“{n}”不在知识库事实中", "删除或改写该表述")
                else:
                    add("warn", where, f"数值“{n}”存在于其他事实点,但未被本段引用的事实支持", "补充对应事实编号")
        if not p.fact_ids and _nums(p.text):
            add("error", where, "含数值的段落没有标注事实依据", "补充依据或删除")
        for x in p.fact_ids:
            f = by_id.get(x)
            if f and norm_model(f.model) != main:
                add("error", where, f"引用了型号“{f.model}”的事实,但主推型号是“{brief.product}”", "改用主推型号的事实")

    for where, text in (("标题", draft.title), ("导语", draft.intro), ("行动引导", draft.cta)):
        for n in _nums(text):
            if n not in all_text:
                add("error", where, f"数值“{n}”不在知识库事实中", "删除或改写")

    # 规格版本号(HDMI 2.1、USB 3.2、蓝牙 5.1……):稿中出现的写法必须能在知识库事实原文里找到
    known_versions = spec_versions("".join(f.text for f in ev.facts))
    for where, text in draft.segments():
        for v in sorted(spec_versions(text) - known_versions):
            add("error", where, f"规格版本“{v}”不在知识库事实中(资料没有写明该版本)", "删除版本号,只写资料中有的内容")

    # 文中出现的其他型号:相近型号必须不混入(先按知识库里的相近型号名查,再用通用写法兜底)
    for where, text in draft.segments():
        for other in mentioned_models(text, ev.candidates):
            add("error", where, f"出现了相近型号“{other}”,主推型号是“{brief.product}”", "核对型号,删除相近型号的内容")
        for tok in MODEL_TOKEN.findall(text):
            t = norm_model(tok)
            if len(t) > 4 and t not in main and main not in t:
                add("error", where, f"出现了非主推型号的写法“{tok.strip()}”", "核对型号")

    images = {i.id: i for i in usable_images(ev, brief.product)}
    known = {i.id: i for i in ev.images}
    for pl in draft.images:
        if pl.image_id not in known:
            add("error", "配图方案", f"图片 {pl.image_id} 不在知识库图片清单中", "移除该图片", pl.image_id)
        elif pl.image_id not in images:
            im = known[pl.image_id]
            add("error", "配图方案", f"图片 {im.id}《{im.name}》无法确认属于主推型号“{brief.product}”", "移除该图片", im.id)
        if not (0 <= pl.after_paragraph <= len(draft.body)):
            add("warn", "配图方案", f"图片 {pl.image_id} 的插入位置超出正文范围", "调整位置")
        if not pl.caption.strip():
            add("warn", "配图方案", f"图片 {pl.image_id} 缺少图片说明", "补充说明")

    limits = strategy.platform.get("images", {})
    n, lo, hi = len(draft.images), limits.get("min", 0), limits.get("max", 99)
    if n > hi:
        add("error", "配图方案", f"图片数量 {n} 超过平台上限 {hi}", "删减图片", "count")
    elif n < lo:
        add("warn", "配图方案", f"图片数量 {n} 少于平台建议的 {lo} 张(可能是知识库可用图片不足)")
    return issues


def llm_check(llm, prompts: Prompts, draft: Draft, ev: Evidence) -> list[Issue]:
    data = llm.json(prompts.render("fact_review", {"稿件": draft_to_json(draft), "事实点清单": facts_block(ev)}))
    return llm_issues(data, "fact")


def check(llm, prompts: Prompts, draft: Draft, ev: Evidence, brief: Brief, strategy: Strategy) -> list[Issue]:
    issues = rule_check(draft, ev, brief, strategy)
    try:
        issues += llm_check(llm, prompts, draft, ev)
    except PromptError as e:      # 提示词还没写好时,规则部分照常工作,并明确提示 LLM 部分被跳过
        issues.append(Issue("fact", "llm", "warn", "事实核验", f"LLM 核验已跳过:{e}"))
    return issues
