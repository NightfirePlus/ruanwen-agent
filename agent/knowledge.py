"""本地知识来源的最小契约与读取异常。"""
from typing import Protocol

from .models import ImageRef


class KnowledgeError(Exception):
    """本地资料不可读、路径非法或索引不完整。"""


class KnowledgeSource(Protocol):
    kb_name: str

    def connect(self) -> dict: ...
    def models(self) -> dict[str, dict]: ...
    def docs(self, model_name: str) -> list[dict]: ...
    def read(self, media_id: str) -> str: ...
    def images(self, model_name: str, limit: int = 30) -> list[ImageRef]: ...
