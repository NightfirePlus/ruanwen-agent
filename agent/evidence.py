"""阶段2:从知识库匹配资料依据。

要点(规则与 LLM 共同把关):
- 型号:先在知识库型号列表里精确匹配;找不到或只有相近型号时不猜,交回去让用户确认。
- 编造:LLM 摘录的事实点,必须逐字出现在资料原文里,否则丢弃(规则)。
- 混淆:摘录中出现其他相近型号名称的,丢弃(规则);资料内部自相矛盾的由 LLM 与规则一起报冲突。
- 来源:每条事实点、每张图片都带资料标题与知识库路径。

真实知识库的目录结构尚未实测,KnowledgeStore 按手册描述实现,联调后可能需要调整。
"""
from __future__ import annotations

import difflib
import re

from .ima import ImaClient
from .models import Evidence, Fact, ImageRef
from .norm import mentioned_models
from .norm import norm_model as norm
from .prompts import Prompts

KB_NAME = "任德耀新建华为PC知识库"
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")
PARAM_FOLDER_HINT = "产品参数"
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


def is_image(title: str) -> bool:
    return (title or "").lower().endswith(IMAGE_EXT)


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


class KnowledgeStore:
    """对 ima 共享知识库的读取视图:型号列表、型号资料、图片。"""

    def __init__(self, ima: ImaClient, kb_name: str = KB_NAME):
        self.ima = ima
        self.kb_name = kb_name
        self.kb_id = ""
        self._models: dict[str, dict] = {}       # 型号名 -> {"folder_id","path"}
        self._folder_names: dict[str, str] = {}  # folder_id -> 路径名

    def connect(self) -> dict:
        """识别账号并找到共享知识库;返回可展示的连接状态(密钥已脱敏)。"""
        hits = self.ima.search_knowledge_base(self.kb_name)
        match = next((h for h in hits if h["kb_name"] == self.kb_name), hits[0] if hits else None)
        status = {"connected": bool(match), "kb_name": self.kb_name, **self.ima.credential_status()}
        if match:
            self.kb_id = match["kb_id"]
        return status

    @staticmethod
    def _is_folder(item: dict) -> bool:
        return bool(item.get("folder_id")) and not item.get("media_id")

    def models(self) -> dict[str, dict]:
        """知识库里的型号列表:产品参数目录下的各子文件夹。"""
        if self._models:
            return self._models
        root = next(
            (it for it in self.ima.list_dir(self.kb_id) if self._is_folder(it) and PARAM_FOLDER_HINT in (it.get("name") or "")),
            None,
        )
        if root:
            self._folder_names[root["folder_id"]] = root["name"]
            for it in self.ima.list_dir(self.kb_id, root["folder_id"]):
                if self._is_folder(it):
                    name = it.get("name") or ""
                    self._models[name] = {"folder_id": it["folder_id"], "path": f"{root['name']}/{name}"}
                    self._folder_names[it["folder_id"]] = name
        return self._models

    def docs(self, model_name: str) -> list[dict]:
        info = self.models()[model_name]
        out = []
        for it in self.ima.list_dir(self.kb_id, info["folder_id"]):
            title = it.get("title") or it.get("name") or ""
            if it.get("media_id") and not is_image(title):
                out.append({"media_id": it["media_id"], "title": title, "path": f"{info['path']}/{title}"})
        return out

    def read(self, media_id: str) -> str:
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
        """图片所属型号由所在目录判定,不由模型猜测。目录外搜到的图片只有标题含型号时才认。"""
        key = norm(model_name)
        info = self.models().get(model_name, {})
        refs: list[ImageRef] = []
        seen: set[str] = set()

        def add(media_id: str, title: str, path: str, belongs: bool) -> None:
            if media_id in seen or len(refs) >= limit:
                return
            seen.add(media_id)
            refs.append(ImageRef(f"I{len(refs) + 1}", model_name if belongs else "", title, media_id, path))

        if info:
            for it in self.ima.list_dir(self.kb_id, info["folder_id"]):
                title = it.get("title") or it.get("name") or ""
                if it.get("media_id") and is_image(title):
                    add(it["media_id"], title, f"{info['path']}/{title}", True)
        for it in self.ima.search(self.kb_id, model_name):
            title = it.get("title", "")
            if it.get("media_id") and is_image(title):
                parent = self._folder_names.get(it.get("parent_folder_id", ""), "")
                add(it["media_id"], title, f"{parent}/{title}" if parent else title, key in norm(title))
        return refs


def detect_conflicts(facts: list[Fact]) -> list[str]:
    """规则检查:同一型号下,同一参数名对应多个不同取值视为冲突。"""
    groups: dict[tuple[str, str, str], set[str]] = {}
    for f in facts:
        for m in NUM_UNIT.findall(f.text):
            before = f.text.split(m)[0]
            label = re.sub(r"[约共达为:：,,\s]", "", re.split(r"[。;;,,]", before)[-1])[-4:]
            unit = re.sub(r"[\d.\s]", "", m).lower()
            groups.setdefault((norm(f.model), label, unit), set()).add(squash(m).lower())
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
