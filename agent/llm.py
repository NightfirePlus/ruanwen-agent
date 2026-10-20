"""OpenAI 兼容接口的大模型封装(阿里云百炼的 deepseek / qwen)。只依赖标准库。

环境变量:
  LLM_API_KEY    必填(也接受 DASHSCOPE_API_KEY)
  LLM_BASE_URL   默认 https://dashscope.aliyuncs.com/compatible-mode/v1
  LLM_MODEL      默认 qwen-plus
Key 只从环境变量读取,错误信息中不出现 Key。
"""
from __future__ import annotations

import json
import os
import re
import time
import threading
import urllib.error
import urllib.request
from collections import deque
from typing import Any

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen-plus"


class LLMError(Exception):
    pass


class RequestBudget:
    """跨客户端共享的 HTTP 请求次数上限，失败请求也计数。"""

    def __init__(self, limit: int):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("请求次数上限必须是正整数")
        self.limit = limit
        self.used = 0
        self._lock = threading.Lock()

    def consume(self):
        with self._lock:
            if self.used >= self.limit:
                raise LLMError(f"已达到本轮请求上限 {self.limit} 次，停止调用")
            self.used += 1


def extract_json(text: str) -> Any:
    """从模型输出中取出 JSON,兼容 ```json 围栏和前后多余文字。"""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        s, e = text.find(open_c), text.rfind(close_c)
        if s != -1 and e > s:
            try:
                return json.loads(text[s : e + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError("模型输出不是合法 JSON")


class LLMClient:
    def __init__(self, api_key=None, base_url=None, model=None, timeout: float = 120.0, max_retries: int = 2,
                 *, max_tokens: int | None = None, enable_thinking: bool | None = None,
                 repair_json: bool = True, budget: RequestBudget | None = None):
        self._api_key = api_key or os.environ.get("LLM_API_KEY") or os.environ.get("DASHSCOPE_API_KEY") or ""
        if not self._api_key:
            raise LLMError("未找到大模型 Key,请设置环境变量 LLM_API_KEY(或 DASHSCOPE_API_KEY)")
        self._base_url = (base_url or os.environ.get("LLM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL
        self._timeout = timeout
        self._max_retries = max_retries
        if max_tokens is not None and (isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1):
            raise ValueError("max_tokens 必须是正整数")
        self._max_tokens = max_tokens
        self._enable_thinking = enable_thinking
        self._repair_json = repair_json
        self._budget = budget
        self.requests: deque[dict] = deque(maxlen=200)  # 仅近期计量，不存凭证、提示词或思考内容
        self._records_lock = threading.Lock()

    def _chat(self, messages: list[dict], temperature: float, json_mode: bool) -> str:
        body: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": temperature}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if self._max_tokens is not None:
            body["max_tokens"] = self._max_tokens
        if self._enable_thinking is not None:
            body["enable_thinking"] = self._enable_thinking
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        last: Exception | None = None
        for attempt in range(self._max_retries + 1):
            if self._budget:
                self._budget.consume()
            record = {"model": self.model, "success": False}
            started = time.monotonic()
            try:
                req = urllib.request.Request(
                    f"{self._base_url}/chat/completions", data=data, headers=headers, method="POST"
                )
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                choice = payload["choices"][0]
                record["usage"] = payload.get("usage", {})
                record["finish_reason"] = choice.get("finish_reason")
                if choice.get("finish_reason") == "length":
                    raise LLMError("模型输出达到长度上限，停止处理截断结果；未自动重试")
                content = choice["message"]["content"]
                if not isinstance(content, str) or not content.strip():
                    raise LLMError("模型未返回有效正文，停止处理；未自动重试")
                record["success"] = True
                return content
            except urllib.error.HTTPError as e:
                last = LLMError(f"大模型接口 HTTP {e.code}")
                if e.code not in (429, 500, 502, 503, 504):
                    raise last
            except (urllib.error.URLError, TimeoutError, KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
                last = LLMError(f"大模型调用失败: {type(e).__name__}")
            finally:
                record["seconds"] = round(time.monotonic() - started, 3)
                with self._records_lock:
                    self.requests.append(record)
            if attempt < self._max_retries:
                time.sleep(1.5 * (attempt + 1))
        assert last is not None
        raise last

    def json(self, user: str | list[dict], system: str | None = None, temperature: float = 0.2) -> Any:
        """要求返回 JSON 并解析;解析失败时带着纠正提示重试一次。system 为 None 时不发系统消息。"""
        messages = [{"role": "system", "content": system}] if system else []
        messages.append({"role": "user", "content": user})
        text = self._chat(messages, temperature, json_mode=True)
        try:
            return extract_json(text)
        except LLMError:
            if not self._repair_json:
                raise
            messages += [
                {"role": "assistant", "content": text},
                {"role": "user", "content": "上面的输出不是合法 JSON。请只输出一个合法 JSON 对象,不要任何其他文字。"},
            ]
            return extract_json(self._chat(messages, 0, json_mode=True))
