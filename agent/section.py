"""小节规划:把平台篇幅目标换算成每个内容块的字数、段数与事实上限。

分块的职责是流水线并行,不是把文章切碎。这里先按平台篇幅算出每块应写的
篇幅,再据此放宽单块可承载的事实条数,并在生成后核对实际字数。

平台没有写具体篇幅时按各平台习惯给出保守目标,避免成稿过短不像宣传稿。
"""
from __future__ import annotations

import re

# 平台未写篇幅时的正文目标字数(公众号正文,不含标题、导语、标签)。
DEFAULT_BODY_CHARS = {"公众号": 1000, "小红书": 420, "朋友圈": 120, "门店海报": 80}

CHARS = re.compile(r"(\d+)\s*[-–~至]\s*(\d+)\s*字|(\d+)\s*字")
SECTIONS = re.compile(r"(\d+)\s*[-–~至]\s*(\d+)\s*(?:个)?小节")


def body_chars(platform: dict) -> tuple[int, int]:
    """平台策略里的正文篇幅目标;取“正文/全文…字”的区间,没写就用默认值。

    朋友圈这类短文案的规则是“全文80-150字”,结构要求另写一句短句,所以取最后一个区间。
    公众号的规则以“正文800-1500字”开头,后面不会再出现别的字数区间。
    """
    text = str(platform.get("length") or "")
    target = (0, 0)
    for lo, hi, single in CHARS.findall(text):
        if single:
            lo = hi = single
        if int(lo) and int(hi) and int(hi) >= int(lo):
            target = (int(lo), int(hi))
    if not target[1]:
        default = DEFAULT_BODY_CHARS.get(platform.get("name", ""), 0)
        return (default, default) if default else (0, 0)
    return target


def section_count(platform: dict, body_chars_range: tuple[int, int]) -> int:
    """平台建议的小节数;公众号没有写小节数时按目标字数给出一个合理的小节数。"""
    text = str(platform.get("length") or "")
    found = SECTIONS.search(text)
    if found:
        return max(1, int(found.group(2)))
    lo, hi = body_chars_range
    if hi < 300:
        return 1
    return max(1, min(5, round(hi / 300)))


def _chunk_chars(platform: dict, blocks: int) -> int:
    """单个正文块的目标字数:平台篇幅上限平均分摊到各正文块。"""
    _, hi = body_chars(platform)
    return max(1, round(hi / max(1, blocks))) if hi else 0


def plan_targets(platform: dict) -> dict:
    """给规划提示词用的篇幅目标。"""
    lo, hi = body_chars(platform)
    sections = section_count(platform, (lo, hi))
    per_section = _chunk_chars(platform, sections)
    # 正文小节少于四段时才建议一段;标准篇幅的小节要写成多段,不能只剩一句话。
    return {
        "正文目标字数": f"{lo}-{hi}字" if lo != hi else f"{hi}字" if hi else "按平台策略",
        "建议正文小节数": sections,
        "单个正文小节建议字数": f"{max(1, round(lo / sections))}-{per_section}字" if per_section else "按平台策略",
        "每块建议段落数": 1 if per_section < 200 else 2,
    }


def paragraphs_per_block(platform: dict, blocks: int | None = None) -> int:
    """单个正文块应有的段落数;短文案一段写完即可。"""
    if not blocks:
        lo, hi = body_chars(platform)
        blocks = section_count(platform, (lo, hi))
    per_section = _chunk_chars(platform, blocks)
    return 1 if not per_section or per_section < 200 else 2


def fact_cap(platform: dict, kind: str) -> int:
    """正文块最多携带的事实条数。

    短文案(朋友圈、海报)保持 2 条以内;标准篇幅放到 4 条;长文正文块可以带 6 条,
    否则一个卖点要挤进堪堪一两句话,读起来像参数清单。
    """
    if kind != "body":
        return 0
    _, hi = body_chars(platform)
    if hi and hi < 300:
        return 2
    if hi and hi >= 800:
        return 6
    return 4


def expected_chars(platform: dict, kind: str, blocks: int | None = None) -> int:
    """单块应达到的正文篇幅;标题、导语和收束不参与篇幅核对。"""
    if kind != "body":
        return 0
    lo, hi = body_chars(platform)
    if not hi:
        return 0
    count = max(1, blocks or section_count(platform, (lo, hi)))
    return max(1, round(hi / count))


def body_blocks(plan) -> int:
    """计划里真正写正文的块数，用来把平台篇幅分摊到每一块。"""
    return sum(1 for block in plan.blocks if block.kind == "body") if plan else 0


def text_length(paragraphs: list[str]) -> int:
    """正文实际字数:只数中文、字母和数字,不计空白与标点。"""
    return len(re.sub(r"[\W_]+", "", "".join(paragraphs), flags=re.UNICODE))


# 篇幅检查只用于“明显不像宣传稿”的情况。写得精炼但信息完整的短稿不应被反复退回，
# 所以阈值取期望字数的三成，并设一个绝对下限。
SHORT_RATIO = 0.15
SHORT_FLOOR = 60


def is_short(platform: dict, kind: str, paragraphs: list[str], blocks: int | None = None) -> bool:
    """正文块篇幅是否明显不足。"""
    expected = expected_chars(platform, kind, blocks)
    if not expected:
        return False
    return text_length(paragraphs) < max(expected * SHORT_RATIO, min(SHORT_FLOOR, expected))


def short_hint(platform: dict, kind: str, paragraphs: list[str], blocks: int | None = None) -> str:
    """篇幅不足时交给重写模型的建议。"""
    expected = expected_chars(platform, kind, blocks)
    return (
        f"正文只有{text_length(paragraphs)}字，篇幅明显偏短，读起来不像宣传稿。"
        f"请把当前块扩写到约{expected}字、至少{paragraphs_per_block(platform, blocks)}段："
        "沿用事实白名单内的事实，把条件、适用场景和读者能得到的信息展开写清楚；"
        "没有事实支撑的内容不得补写，改为把已有事实讲透。"
    )