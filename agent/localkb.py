"""本地知识库:直接读磁盘上的知识库目录,不访问 ima。

和 evidence.KnowledgeStore 提供同一组方法(connect / models / docs / read / images),流水线不需要区分。
读取范围与 ima 版本一致:只认 P1-产品参数库 下含 产品事实.md 的目录是一个型号,
读取其中的 产品事实.md 与 卖点与限制.md;图片取自该型号目录的 images/,所属型号以 图片索引.csv 为准。
P2~P8、竞品、价格(机密)、待审核、历史归档都不读。

安全:只读;所有路径都解析后检查必须落在知识库根目录内,避免被目录里的 .. 或符号链接带出去。
"""
from __future__ import annotations

from pathlib import Path

from .evidence import (
    FACT_FILES,
    IMAGE_DIR,
    IMAGE_INDEX,
    MAX_SERIES_DEPTH,
    PARAM_ROOT,
    official_name,
    parse_image_index,
    same_product,
)
from .models import ImageRef

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")


class LocalKBError(Exception):
    pass


class LocalStore:
    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        self.kb_name = self.root.name
        self.kb_id = str(self.root)          # 流水线用它判断“是否已连接”
        self.cache_hits = {"index": False, "docs": 0}
        self._models: dict[str, dict] = {}

    # ---- 路径安全 ----
    def _safe(self, p: Path) -> Path:
        r = p.resolve()
        if r != self.root and self.root not in r.parents:
            raise LocalKBError(f"路径越出知识库目录:{p}")
        return r

    # ---- 与 KnowledgeStore 同名的接口 ----
    def connect(self) -> dict:
        ok = (self.root / PARAM_ROOT).is_dir()
        return {"connected": ok, "kb_name": self.kb_name, "client_id": "(本地)", "api_key": "(本地)"}

    def models(self) -> dict[str, dict]:
        if self._models:
            return self._models
        p1 = self.root / PARAM_ROOT
        if not p1.is_dir():
            return self._models

        def walk(folder: Path, trail: list[str], depth: int) -> None:
            try:
                entries = sorted(folder.iterdir(), key=lambda x: x.name)
            except OSError:
                return
            names = {e.name for e in entries}
            if FACT_FILES[0] in names and trail:
                self._models[trail[-1]] = {
                    "folder_id": str(folder),
                    "path": "/".join([PARAM_ROOT] + trail),
                    "children": {e.name: {"media_id": str(e), "title": e.name} for e in entries},
                }
                return
            if depth >= MAX_SERIES_DEPTH:
                return
            for e in entries:
                if e.is_dir() and not e.is_symlink():
                    walk(e, trail + [e.name], depth + 1)

        walk(p1, [], 0)
        return self._models

    def docs(self, model_name: str) -> list[dict]:
        info = self.models()[model_name]
        out = []
        for fname in FACT_FILES:
            it = info["children"].get(fname)
            if it:
                out.append({"media_id": it["media_id"], "title": fname, "path": f"{info['path']}/{fname}"})
        return out

    def read(self, media_id: str) -> str:
        path = self._safe(Path(media_id))
        if not path.is_file():
            return ""
        raw = path.read_bytes()
        for enc in ("utf-8-sig", "gb18030"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    def official(self, model_name: str) -> str:
        info = self.models()[model_name]
        if "official" not in info:
            fact = info["children"].get(FACT_FILES[0])
            info["official"] = official_name(self.read(fact["media_id"]) if fact else "", model_name)
        return info["official"]

    def images(self, model_name: str, limit: int = 30) -> list[ImageRef]:
        info = self.models()[model_name]
        official = self.official(model_name)
        folder = Path(info["folder_id"]) / IMAGE_DIR
        if not folder.is_dir():
            return []
        index = {}
        idx = info["children"].get(IMAGE_INDEX)
        if idx:
            index = parse_image_index(self.read(idx["media_id"]))
        refs: list[ImageRef] = []
        for f in sorted(folder.iterdir(), key=lambda x: x.name):
            if not f.is_file() or f.suffix.lower() not in IMAGE_EXT or len(refs) >= limit:
                continue
            row = index.get(f.name)
            if row is None:
                belongs, note = False, "图片索引中未登记"
            else:
                listed = row.get("产品型号", "")
                belongs = same_product(listed, model_name, official)
                note = "" if belongs else f"索引登记的型号为“{listed}”"
            refs.append(
                ImageRef(
                    f"I{len(refs) + 1}",
                    model_name if belongs else "",
                    f.name,
                    str(f),
                    f"{info['path']}/{IMAGE_DIR}/{f.name}",
                    caption=(row or {}).get("官网图片说明", ""),
                    usage=(row or {}).get("建议用途", ""),
                    origin_url=(row or {}).get("原始图片URL", ""),
                    note=note,
                )
            )
        return refs

    def refresh(self) -> None:
        self._models = {}
