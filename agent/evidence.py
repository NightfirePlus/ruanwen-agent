"""阶段2:从知识库匹配资料依据。

要点(规则与 LLM 共同把关):
- 型号:先在知识库型号列表里精确匹配;找不到或只有相近型号时不猜,交回去让用户确认。
- 编造:LLM 摘录的事实点,必须逐字出现在资料原文里,否则丢弃(规则)。
- 混淆:摘录中出现其他相近型号名称的,丢弃(规则);资料内部自相矛盾的由 LLM 与规则一起报冲突。
- 来源:每条事实点、每张图片都带资料标题与知识库路径。

目录结构已用真实知识库探查(见 KnowledgeStore 说明)。
ima 的“浏览目录”接口有调用额度(用尽返回 220021),因此型号表与资料正文落到本地 .cache/,
命中缓存时不再访问接口;额度用尽且没有缓存时,抛出 ImaQuotaError 并给出明确提示。
"""
from __future__ import annotations

import csv
import difflib
import io
import json
import re
from pathlib import Path

from .ima import ImaClient, ImaQuotaError
from .models import Evidence, Fact, ImageRef
from .norm import mentioned_models
from .norm import norm_model as norm
from .prompts import Prompts

KB_NAME = "任德耀新建华为PC知识库"
PARAM_ROOT = "P1-产品参数库"           # 事实层只认这个目录
FACT_FILES = ("产品事实.md", "卖点与限制.md")   # 一个型号目录里读取的两份资料
IMAGE_INDEX = "图片索引.csv"
IMAGE_DIR = "images"
FOLDER = 99                            # ima 里文件夹的 media_type
IMAGE_TYPE = 9
MAX_SERIES_DEPTH = 3
MAX_DOCS = 4
CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache"
INDEX_VERSION = 1

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


def is_folder(item: dict) -> bool:
    return item.get("media_type") == FOLDER


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
    return {r["文件名"].strip(): r for r in rows if r.get("文件名")}


class KnowledgeStore:
    """对 ima 共享知识库的只读视图。

    事实层:P1-产品参数库 下,含 产品事实.md 的目录就是一个型号(不假设层级深度)。
    其余目录(P2~P8、竞品、价格、待审核、历史归档)不在这里读取。
    """

    def __init__(self, ima: ImaClient, kb_name: str = KB_NAME, cache_dir: Path | None = CACHE_DIR):
        self.ima = ima
        self.kb_name = kb_name
        self.kb_id = ""
        self.cache_dir = cache_dir           # 为 None 时不使用缓存
        self.cache_hits = {"index": False, "docs": 0}
        self._models: dict[str, dict] = {}   # 型号名 -> {"folder_id","path","children": {标题: 条目}}

    # ---- 本地缓存 ----
    def _cache_file(self, name: str) -> Path | None:
        return (self.cache_dir / name) if self.cache_dir else None

    def _cache_read(self, name: str) -> str | None:
        f = self._cache_file(name)
        return f.read_text(encoding="utf-8") if f and f.is_file() else None

    def _cache_write(self, name: str, text: str) -> None:
        f = self._cache_file(name)
        if f:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text, encoding="utf-8")

    def refresh(self) -> None:
        """丢弃本地型号表,下次使用时重新浏览目录(会消耗浏览额度)。"""
        self._models = {}
        f = self._cache_file("kb_index.json")
        if f and f.is_file():
            f.unlink()

    def connect(self) -> dict:
        """识别账号并找到共享知识库;返回可展示的连接状态(密钥已脱敏)。"""
        hits = self.ima.search_knowledge_base(self.kb_name)
        match = next((h for h in hits if h["kb_name"] == self.kb_name), hits[0] if hits else None)
        status = {"connected": bool(match), "kb_name": self.kb_name, **self.ima.credential_status()}
        if match:
            self.kb_id = match["kb_id"]
        return status

    def official(self, model_name: str) -> str:
        """读取该型号 产品事实.md 的标题作为官方全名(结果缓存在型号表里)。"""
        info = self.models()[model_name]
        if "official" not in info:
            fact = info["children"].get(FACT_FILES[0])
            info["official"] = official_name(self.read(fact["media_id"]) if fact else "", model_name)
        return info["official"]

    def _children(self, folder_id: str) -> list[dict]:
        return self.ima.list_dir(self.kb_id, folder_id)

    def _root_folder(self) -> str:
        """知识库根在列表里表现为一个与库同名的文件夹条目。"""
        top = self._children(None)
        named = next((i for i in top if is_folder(i) and i.get("title") == self.kb_name), None)
        return (named or (top[0] if top else {})).get("media_id", "")

    def models(self) -> dict[str, dict]:
        """扫描 P1:凡含 产品事实.md 的目录即为一个型号。"""
        if self._models:
            return self._models
        cached = self._cache_read("kb_index.json")
        if cached:
            data = json.loads(cached)
            if data.get("version") == INDEX_VERSION and data.get("kb_name") == self.kb_name:
                self._models = data["models"]
                self.cache_hits["index"] = True
                return self._models
        self._scan()
        if self._models:
            self._cache_write(
                "kb_index.json",
                json.dumps({"version": INDEX_VERSION, "kb_name": self.kb_name, "models": self._models}, ensure_ascii=False),
            )
        return self._models

    def _scan(self) -> None:
        root = self._root_folder()
        p1 = next((i for i in self._children(root) if is_folder(i) and i.get("title") == PARAM_ROOT), None)
        if not p1:
            return

        def walk(folder_id: str, trail: list[str], depth: int) -> None:
            items = self._children(folder_id)
            by_title = {i.get("title", ""): i for i in items}
            if FACT_FILES[0] in by_title and trail:
                name = trail[-1]
                self._models[name] = {"folder_id": folder_id, "path": "/".join([PARAM_ROOT] + trail), "children": by_title}
                return
            if depth >= MAX_SERIES_DEPTH:
                return
            for it in items:
                if is_folder(it):
                    walk(it["media_id"], trail + [it["title"]], depth + 1)

        walk(p1["media_id"], [], 0)

    def docs(self, model_name: str) -> list[dict]:
        """一个型号的资料:产品事实 + 卖点与限制。"""
        info = self.models()[model_name]
        out = []
        for fname in FACT_FILES:
            it = info["children"].get(fname)
            if it:
                out.append({"media_id": it["media_id"], "title": fname, "path": f"{info['path']}/{fname}"})
        return out

    def read(self, media_id: str) -> str:
        safe = re.sub(r"[^\w.-]", "_", media_id)[:120]
        cached = self._cache_read(f"docs/{safe}.txt")
        if cached is not None:
            self.cache_hits["docs"] += 1
            return cached
        text = self._download(media_id)
        if text:
            self._cache_write(f"docs/{safe}.txt", text)
        return text

    def _download(self, media_id: str) -> str:
        raw = self.ima.fetch(media_id)
        if raw is None:
            return ""
        for enc in ("utf-8", "gb18030"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    def images(self, model_name: str, limit: int = 30) -> list[ImageRef]:
        """图片取自型号目录的 images/。所属型号以 图片索引.csv 的“产品型号”为准,索引缺失时才按目录归属;
        索引里写的型号与目录型号对不上的图片,型号置空(视为未确认)。"""
        info = self.models()[model_name]
        folder = info["children"].get(IMAGE_DIR)
        if not folder:
            return []
        index: dict[str, dict] = {}
        idx_item = info["children"].get(IMAGE_INDEX)
        if idx_item:
            index = parse_image_index(self.read(idx_item["media_id"]))
        refs: list[ImageRef] = []
        for it in self._children(folder["media_id"]):
            if it.get("media_type") != IMAGE_TYPE or len(refs) >= limit:
                continue
            title = it.get("title", "")
            row = index.get(title)
            if row is None:
                belongs, note = False, "图片索引中未登记"
            else:
                listed = row.get("产品型号", "")
                belongs = self._same_product(listed, model_name, self.official(model_name))
                note = "" if belongs else f"索引登记的型号为“{listed}”"
            refs.append(
                ImageRef(
                    f"I{len(refs) + 1}",
                    model_name if belongs else "",
                    title,
                    it["media_id"],
                    f"{info['path']}/{IMAGE_DIR}/{title}",
                    caption=(row or {}).get("官网图片说明", ""),
                    usage=(row or {}).get("建议用途", ""),
                    origin_url=(row or {}).get("原始图片URL", ""),
                    note=note,
                )
            )
        return refs

    @staticmethod
    def _same_product(listed: str, model_name: str, official: str = "") -> bool:
        return same_product(listed, model_name, official)


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


def build(llm, prompts: Prompts, store: KnowledgeStore, model: str) -> Evidence:
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
