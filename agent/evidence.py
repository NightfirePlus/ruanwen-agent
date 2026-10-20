"""阶段2：从本地知识库精确匹配型号、摘录事实并校验图片来源。

事实必须逐字出现在资料中；相近型号不能混用；每条事实保留来源路径。
"""
from __future__ import annotations

import csv
import difflib
import io
import re

from .knowledge import KnowledgeError, KnowledgeSource
from .models import Evidence, Fact
from .norm import mentioned_models
from .norm import norm_model as norm
from .prompts import Prompts

PARAM_ROOT = "P1-产品参数库"           # 事实层只认这个目录
FACT_FILES = ("产品事实.md", "卖点与限制.md")   # 一个型号目录里读取的两份资料
IMAGE_INDEX = "图片索引.csv"
IMAGE_DIR = "images"
MAX_SERIES_DEPTH = 3
MAX_DOCS = 4

NUM_UNIT = re.compile(
    r"\d+(?:\.\d+)?\s*(?:kg|g|GB|TB|MB|Hz|W|Wh|mAh|mm|cm|英寸|寸|小时|h|分钟|nm|核|线程|万|K|%|倍|代|瓦|克|毫米)",
    re.I,
)


class ModelNotResolved(Exception):
    """知识库里没有与输入完全一致的型号。candidates 是相近型号,可能为空。"""

    def __init__(self, model: str, candidates: list[str]):
        super().__init__(model)
        self.model = model
        self.candidates = candidates


def squash(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def similar(model: str, names: list[str], cutoff: float = 0.6) -> list[str]:
    """相近型号:归一化后互为前缀,或字符相似度达到阈值。排除完全一致者。"""
    key = norm(model)
    out = []
    for n in names:
        k = norm(n)
        if not k or k == key:
            continue
        if k.startswith(key) or key.startswith(k) or difflib.SequenceMatcher(None, key, k).ratio() >= cutoff:
            out.append(n)
    return out


def official_name(fact_text: str, fallback: str) -> str:
    """型号的官方全名:取 产品事实.md 的一级标题,去掉末尾的“ - 产品事实”。
    目录名常是简称(“MateBook D 14”),资料里的正式名称是“HUAWEI MateBook D 14 2024”,
    图片索引登记的是后者,所以图片归属要和官方全名比,而不是和目录名比。"""
    for line in (fact_text or "").splitlines():
        line = line.strip()
        if line.startswith("# "):
            name = re.sub(r"\s*[-－—]\s*产品事实\s*$", "", line[2:].strip())
            return name or fallback
    return fallback


def same_product(listed: str, *names: str) -> bool:
    """索引里的型号是否就是这个产品。去品牌前缀、空格、连字符、全半角括号后做精确比较,
    不做子串匹配:这样 “MateBook 14” 与 “MateBook 14 酷睿 Ultra” 不会互相认成同一款。"""
    def key(s: str) -> str:
        s = (s or "").replace("（", "(").replace("）", ")")
        return norm(s.replace("HUAWEI", ""))
    k = key(listed)
    return bool(k) and any(k == key(n) for n in names if n)


def parse_image_index(text: str) -> dict[str, dict]:
    """图片索引.csv -> {文件名: 行}。表头固定含:文件名,产品型号,来源页面,原始图片URL,官网图片说明,建议用途。"""
    rows = csv.DictReader(io.StringIO(text.lstrip(chr(0xFEFF))))
    if not rows.fieldnames or not {"文件名", "产品型号"}.issubset(rows.fieldnames):
        raise KnowledgeError("图片索引缺少文件名或产品型号字段")
    result = {}
    for row in rows:
        name = (row.get("文件名") or "").strip()
        if not name:
            continue
        if name in result:
            raise KnowledgeError(f"图片索引同一文件重复登记，需人工确认：{name}")
        result[name] = row
    return result


def detect_conflicts(facts: list[Fact]) -> list[str]:
    """高精度规则检查:相同句式中的单一规格值不一致才视为冲突。

    不能只按“参数名+单位”归组:同一产品常同时列出多种色域模式、频段或供电档位,
    它们有多个合法数值。这里把整条分句去掉数值后作为语义签名；包含多个规格值的
    分句交给 LLM 判断，宁可少报也不因多档规格阻断整条流水线。
    """
    groups: dict[tuple[str, str, str], set[str]] = {}
    for f in facts:
        for clause in re.split(r"[。；;，,\n]", f.text):
            matches = list(NUM_UNIT.finditer(clause))
            if len(matches) != 1:
                continue
            m = matches[0]
            value = squash(m.group(0)).lower()
            unit = re.sub(r"[\d.\s]", "", m.group(0)).lower()
            signature = squash(clause[:m.start()] + "<值>" + clause[m.end():]).lower()
            if not signature or signature == "<值>":
                continue
            groups.setdefault((norm(f.model), signature, unit), set()).add(value)
    return [f"型号 {k[0]} 的“{k[1]}”出现多个取值:{'、'.join(sorted(v))}" for k, v in groups.items() if len(v) > 1]


def build(llm, prompts: Prompts, store: KnowledgeSource, model: str) -> Evidence:
    """为一个型号生成资料依据。型号无法确认时抛出 ModelNotResolved。"""
    names = list(store.models())
    exact = next((n for n in names if norm(n) == norm(model)), None)
    if exact is None:
        raise ModelNotResolved(model, similar(model, names))
    others = [n for n in similar(exact, names)]

    ev = Evidence(candidates=others)
    for doc in store.docs(exact)[:MAX_DOCS]:
        content = store.read(doc["media_id"])
        if not content.strip():
            continue
        ev.sources.append(f"{doc['path']}")
        data = llm.json(
            prompts.render("evidence", {"型号": exact, "资料标题": doc["title"], "资料全文": content})
        )
        source = squash(content)
        seen: set[str] = set()
        for item in data.get("facts", []):
            text = (item.get("text") if isinstance(item, dict) else str(item) or "").strip()
            key = squash(text)
            if not key or key in seen:
                continue
            if key not in source:                                  # 编造:不在原文中
                ev.dropped.append(f"不在资料原文中:{text}")
                continue
            if mentioned_models(text, names) - {exact}:              # 混淆:提到了别的型号(含相近型号)
                ev.dropped.append(f"提到其他型号:{text}")
                continue
            seen.add(key)
            ev.facts.append(
                Fact(f"F{len(ev.facts) + 1}", exact, text, doc["title"], doc["path"], doc["media_id"])
            )
        ev.conflicts += [str(c) for c in data.get("conflicts", []) if c]
    ev.conflicts += detect_conflicts(ev.facts)
    ev.conflicts = list(dict.fromkeys(ev.conflicts))
    ev.images = store.images(exact)
    return ev
