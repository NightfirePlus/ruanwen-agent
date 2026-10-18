"""ima OpenAPI 封装。

只依赖标准库。凭证只从环境变量读取,日志与异常信息中一律脱敏。
每次调用都会写入 call_log,供软文生成时追溯资料与图片来源。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

BASE_URL = "https://ima.qq.com"
WIKI_PATH = "openapi/wiki/v1"
ENV_CLIENT_ID = "IMA_OPENAPI_CLIENTID"
ENV_API_KEY = "IMA_OPENAPI_APIKEY"

# 110010 为下游网络错误,可重试;110021 为频控,降频后重试
RETRYABLE_CODES = {110010, 110021}


class ImaError(Exception):
    """ima 调用失败。message 中不会包含任何凭证。"""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def mask_secret(value: str, head: int = 3, tail: int = 2) -> str:
    """脱敏显示,例如 o+G***==。过短的值全部遮蔽。"""
    if not value:
        return ""
    if len(value) <= head + tail + 2:
        return "*" * len(value)
    return f"{value[:head]}***{value[-tail:]}"


@dataclass
class CallRecord:
    """单次接口调用的追溯记录(不含凭证)。"""

    endpoint: str
    request: dict[str, Any]
    code: int | None
    msg: str
    elapsed_ms: int
    at: float = field(default_factory=time.time)


class ImaClient:
    def __init__(
        self,
        client_id: str | None = None,
        api_key: str | None = None,
        timeout: float = 20.0,
        max_retries: int = 2,
        base_url: str = BASE_URL,
    ):
        self._client_id = client_id or os.environ.get(ENV_CLIENT_ID, "")
        self._api_key = api_key or os.environ.get(ENV_API_KEY, "")
        if not self._client_id or not self._api_key:
            raise ImaError(
                f"未找到 ima 凭证,请设置环境变量 {ENV_CLIENT_ID} 和 {ENV_API_KEY}"
            )
        self._timeout = timeout
        self._max_retries = max_retries
        self._base_url = base_url.rstrip("/")
        self.call_log: list[CallRecord] = []

    # ---- 账号状态(用于界面展示,绝不返回完整密钥) ----
    def credential_status(self) -> dict[str, str]:
        return {
            "client_id": mask_secret(self._client_id),
            "api_key": mask_secret(self._api_key),
        }

    # ---- 底层请求 ----
    def _post(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}/{WIKI_PATH}/{endpoint}"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {
            "ima-openapi-clientid": self._client_id,
            "ima-openapi-apikey": self._api_key,
            "Content-Type": "application/json",
        }
        last_err: ImaError | None = None
        for attempt in range(self._max_retries + 1):
            started = time.monotonic()
            try:
                req = urllib.request.Request(url, data=data, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                last_err = ImaError(f"{endpoint} HTTP {e.code}", code=e.code)
                self._record(endpoint, body, e.code, f"HTTP {e.code}", started)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                last_err = ImaError(f"{endpoint} 请求失败: {type(e).__name__}")
                self._record(endpoint, body, None, type(e).__name__, started)
            else:
                code = payload.get("code")
                msg = str(payload.get("msg", ""))
                self._record(endpoint, body, code, msg, started)
                if code == 0:
                    return payload.get("data") or {}
                last_err = ImaError(f"{endpoint} 失败: {msg}", code=code)
                if code not in RETRYABLE_CODES:
                    raise last_err
            if attempt < self._max_retries:
                time.sleep(0.8 * (attempt + 1))
        assert last_err is not None
        raise last_err

    def _record(self, endpoint, body, code, msg, started) -> None:
        self.call_log.append(
            CallRecord(
                endpoint=endpoint,
                request=body,
                code=code,
                msg=msg,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        )

    # ---- 四个业务接口 ----
    def search_knowledge_base(self, query: str, limit: int = 20, cursor: str = "") -> list[dict]:
        """按名称查找知识库。兼容 id/name 与 kb_id/kb_name 两种返回字段。"""
        data = self._post(
            "search_knowledge_base", {"query": query, "cursor": cursor, "limit": limit}
        )
        result = []
        for item in data.get("info_list") or []:
            result.append(
                {
                    "kb_id": item.get("kb_id") or item.get("id") or "",
                    "kb_name": item.get("kb_name") or item.get("name") or "",
                }
            )
        return result

    def get_knowledge_list(
        self,
        knowledge_base_id: str,
        folder_id: str | None = None,
        limit: int = 50,
        cursor: str = "",
    ) -> dict:
        """浏览目录。返回 {items, folders, is_end, next_cursor, current_path}。"""
        body: dict[str, Any] = {
            "knowledge_base_id": knowledge_base_id,
            "cursor": cursor,
            "limit": limit,
        }
        if folder_id:
            body["folder_id"] = folder_id
        data = self._post("get_knowledge_list", body)
        return {
            "items": data.get("knowledge_list") or [],
            "is_end": bool(data.get("is_end", True)),
            "next_cursor": data.get("next_cursor", ""),
            "current_path": data.get("current_path") or [],
        }

    def list_all(self, knowledge_base_id: str, folder_id: str | None = None) -> list[dict]:
        """翻完一个目录下的全部条目。"""
        out: list[dict] = []
        cursor = ""
        while True:
            page = self.get_knowledge_list(knowledge_base_id, folder_id, cursor=cursor)
            out.extend(page["items"])
            if page["is_end"] or not page["next_cursor"]:
                return out
            cursor = page["next_cursor"]

    def search_knowledge(self, knowledge_base_id: str, query: str, cursor: str = "") -> dict:
        """按型号/关键词搜索。搜索为空不代表无权限,调用方应回退到目录浏览。"""
        data = self._post(
            "search_knowledge",
            {"query": query, "cursor": cursor, "knowledge_base_id": knowledge_base_id},
        )
        return {
            "items": data.get("info_list") or [],
            "is_end": bool(data.get("is_end", True)),
            "next_cursor": data.get("next_cursor", ""),
        }

    def get_media_info(self, media_id: str) -> dict:
        """获取资料原文或图片的访问信息。url_info 为空表示只能在 ima 客户端查看。"""
        if not media_id:
            raise ImaError("media_id 不能为空")
        return self._post("get_media_info", {"media_id": media_id})

    def fetch_media_bytes(self, media_id: str, max_bytes: int = 10 * 1024 * 1024) -> bytes | None:
        """按 get_media_info 返回的 url 与 headers 拉取内容;不可访问时返回 None。"""
        info = self.get_media_info(media_id)
        url_info = info.get("url_info") or {}
        url = url_info.get("url")
        if not url:
            return None
        req = urllib.request.Request(url, headers=url_info.get("headers") or {})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return resp.read(max_bytes + 1)[:max_bytes]
        except (urllib.error.URLError, TimeoutError):
            return None
