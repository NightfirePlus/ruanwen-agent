"""ima OpenAPI 封装。只依赖标准库。

凭证只从环境变量 IMA_OPENAPI_CLIENTID / IMA_OPENAPI_APIKEY 读取;日志、异常、界面状态中一律脱敏。
接口约定:POST https://ima.qq.com/openapi/wiki/v1/{endpoint},JSON body,
请求头 ima-openapi-clientid / ima-openapi-apikey。
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
RETRYABLE = {110010, 110021}   # 下游网络错误、请求频控


class ImaError(Exception):
    """ima 调用失败。信息中不含任何凭证。"""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def mask_secret(value: str, head: int = 3, tail: int = 2) -> str:
    if not value:
        return ""
    if len(value) <= head + tail + 2:
        return "*" * len(value)
    return f"{value[:head]}***{value[-tail:]}"


@dataclass
class CallRecord:
    endpoint: str
    request: dict[str, Any]
    code: int | None
    msg: str
    elapsed_ms: int
    at: float = field(default_factory=time.time)


class ImaClient:
    def __init__(self, client_id=None, api_key=None, timeout: float = 20.0, max_retries: int = 2, base_url: str = BASE_URL):
        self._client_id = client_id or os.environ.get(ENV_CLIENT_ID, "")
        self._api_key = api_key or os.environ.get(ENV_API_KEY, "")
        if not self._client_id or not self._api_key:
            raise ImaError(f"未找到 ima 凭证,请设置环境变量 {ENV_CLIENT_ID} 和 {ENV_API_KEY}")
        self._timeout = timeout
        self._max_retries = max_retries
        self._base_url = base_url.rstrip("/")
        self.call_log: list[CallRecord] = []

    def credential_status(self) -> dict[str, str]:
        """用于界面展示,绝不返回完整密钥。"""
        return {"client_id": mask_secret(self._client_id), "api_key": mask_secret(self._api_key)}

    def _post(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}/{WIKI_PATH}/{endpoint}"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {
            "ima-openapi-clientid": self._client_id,
            "ima-openapi-apikey": self._api_key,
            "Content-Type": "application/json",
        }
        last: ImaError | None = None
        for attempt in range(self._max_retries + 1):
            started = time.monotonic()
            try:
                req = urllib.request.Request(url, data=data, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                last = ImaError(f"{endpoint} HTTP {e.code}", code=e.code)
                self._record(endpoint, body, e.code, f"HTTP {e.code}", started)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                last = ImaError(f"{endpoint} 请求失败: {type(e).__name__}")
                self._record(endpoint, body, None, type(e).__name__, started)
            else:
                code, msg = payload.get("code"), str(payload.get("msg", ""))
                self._record(endpoint, body, code, msg, started)
                if code == 0:
                    return payload.get("data") or {}
                last = ImaError(f"{endpoint} 失败: {msg}", code=code)
                if code not in RETRYABLE:
                    raise last
            if attempt < self._max_retries:
                time.sleep(0.8 * (attempt + 1))
        assert last is not None
        raise last

    def _record(self, endpoint, body, code, msg, started) -> None:
        self.call_log.append(CallRecord(endpoint, body, code, msg, int((time.monotonic() - started) * 1000)))

    # ---- 业务接口 ----
    def search_knowledge_base(self, query: str, limit: int = 20) -> list[dict]:
        """按名称查找知识库。兼容 id/name 与 kb_id/kb_name 两种返回字段。"""
        data = self._post("search_knowledge_base", {"query": query, "cursor": "", "limit": limit})
        return [
            {"kb_id": it.get("kb_id") or it.get("id") or "", "kb_name": it.get("kb_name") or it.get("name") or ""}
            for it in data.get("info_list") or []
        ]

    def list_dir(self, kb_id: str, folder_id: str | None = None) -> list[dict]:
        """列出一个目录下的全部条目(自动翻页)。"""
        out: list[dict] = []
        cursor = ""
        while True:
            body: dict[str, Any] = {"knowledge_base_id": kb_id, "cursor": cursor, "limit": 50}
            if folder_id:
                body["folder_id"] = folder_id
            data = self._post("get_knowledge_list", body)
            out += data.get("knowledge_list") or []
            if data.get("is_end", True) or not data.get("next_cursor"):
                return out
            cursor = data["next_cursor"]

    def search(self, kb_id: str, query: str) -> list[dict]:
        """按关键词搜索(自动翻页)。搜索为空不代表无权限,调用方应回退到目录浏览。"""
        out: list[dict] = []
        cursor = ""
        while True:
            data = self._post("search_knowledge", {"query": query, "cursor": cursor, "knowledge_base_id": kb_id})
            out += data.get("info_list") or []
            if data.get("is_end", True) or not data.get("next_cursor"):
                return out
            cursor = data["next_cursor"]

    def media_info(self, media_id: str) -> dict:
        if not media_id:
            raise ImaError("media_id 不能为空")
        return self._post("get_media_info", {"media_id": media_id})

    def fetch(self, media_id: str, max_bytes: int = 10 * 1024 * 1024) -> bytes | None:
        """按 media_info 返回的 url 与 headers 拉取内容;不可访问时返回 None。"""
        url_info = self.media_info(media_id).get("url_info") or {}
        url = url_info.get("url")
        if not url:
            return None
        req = urllib.request.Request(url, headers=url_info.get("headers") or {})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return resp.read(max_bytes + 1)[:max_bytes]
        except (urllib.error.URLError, TimeoutError):
            return None
