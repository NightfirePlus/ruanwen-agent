"""检索:定位知识库,按型号取资料与图片。

真实知识库的目录结构尚未实测(见 README 待办),这里按手册描述实现并保持容错:
搜索为空时回退到目录浏览,进入"产品参数"目录读取具体型号的 article.md。
"""
from __future__ import annotations

import re

from ima import ImaClient

from .models import ImageRef

KB_NAME = "任德耀新建华为PC知识库"
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")
PARAM_FOLDER_HINT = "产品参数"


def norm(s: str) -> str:
    """型号归一化:小写,去空格、连字符、下划线。"""
    return re.sub(r"[\s\-_·]", "", (s or "").lower())


def is_image(title: str) -> bool:
    return (title or "").lower().endswith(IMAGE_EXT)


class Retriever:
    def __init__(self, ima: ImaClient, kb_name: str = KB_NAME):
        self.ima = ima
        self.kb_name = kb_name
        self.kb_id = ""

    def connect(self) -> dict:
        """识别账号并找到共享知识库;返回可展示的连接状态(密钥已脱敏)。"""
        hits = self.ima.search_knowledge_base(self.kb_name)
        match = next((h for h in hits if h["kb_name"] == self.kb_name), hits[0] if hits else None)
        if not match:
            return {"connected": False, "kb_name": self.kb_name, **self.ima.credential_status()}
        self.kb_id = match["kb_id"]
        return {"connected": True, "kb_name": match["kb_name"], **self.ima.credential_status()}

    # ---- 目录工具 ----
    @staticmethod
    def _is_folder(item: dict) -> bool:
        return bool(item.get("folder_id")) and not item.get("media_id")

    def _find_folder(self, parent: str | None, hint: str) -> str | None:
        for it in self.ima.list_all(self.kb_id, parent):
            name = it.get("name") or it.get("title") or ""
            if self._is_folder(it) and hint in name:
                return it["folder_id"]
        return None

    # ---- 资料 ----
    def find_docs(self, model: str) -> list[dict]:
        """返回与型号相关的资料条目 [{media_id,title,folder_id}]。"""
        docs = self._search(model)
        if docs:
            return docs
        # 搜索为空不等于无权限:回退到目录浏览
        folder = self._find_folder(None, PARAM_FOLDER_HINT)
        if not folder:
            return []
        return self._browse(folder, model)

    def _search(self, query: str) -> list[dict]:
        out: list[dict] = []
        cursor = ""
        while True:
            page = self.ima.search_knowledge(self.kb_id, query, cursor)
            for it in page["items"]:
                if it.get("media_id") and not is_image(it.get("title", "")):
                    out.append(
                        {
                            "media_id": it["media_id"],
                            "title": it.get("title", ""),
                            "folder_id": it.get("parent_folder_id", ""),
                        }
                    )
            if page["is_end"] or not page["next_cursor"]:
                return out
            cursor = page["next_cursor"]

    def _browse(self, folder_id: str, model: str, depth: int = 2) -> list[dict]:
        out: list[dict] = []
        key = norm(model)
        for it in self.ima.list_all(self.kb_id, folder_id):
            title = it.get("title") or it.get("name") or ""
            if self._is_folder(it):
                if depth > 0 and (key in norm(title) or depth == 2):
                    out += self._browse(it["folder_id"], model, depth - 1)
            elif it.get("media_id") and not is_image(title):
                out.append({"media_id": it["media_id"], "title": title, "folder_id": folder_id})
        # 目录里的文件需要和型号相关才保留;文件夹名也算相关
        return [d for d in out if key in norm(d["title"]) or key in norm(self._path_name(d))] or out

    def _path_name(self, doc: dict) -> str:
        return doc.get("folder_name", "")

    def read_doc(self, media_id: str) -> str:
        raw = self.ima.fetch_media_bytes(media_id)
        if raw is None:
            return ""
        for enc in ("utf-8", "gb18030"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    # ---- 图片 ----
    def find_images(self, model: str, limit: int = 30) -> list[ImageRef]:
        """搜索与型号相关的图片。图片所属型号由标题/目录名判定,不由模型猜测。"""
        key = norm(model)
        refs: list[ImageRef] = []
        seen: set[str] = set()
        page = self.ima.search_knowledge(self.kb_id, model)
        for it in page["items"]:
            title = it.get("title", "")
            mid = it.get("media_id", "")
            if not mid or mid in seen or not is_image(title):
                continue
            seen.add(mid)
            belongs = key in norm(title)
            refs.append(
                ImageRef(
                    id=f"I{len(refs) + 1}",
                    model=model if belongs else "未确认",
                    name=title,
                    media_id=mid,
                    source=f"{self.kb_name}/{title}",
                    folder=it.get("parent_folder_id", ""),
                )
            )
            if len(refs) >= limit:
                break
        return refs
