"""生产运行所需的日志、指标和轻量告警；不记录凭证或模型输入输出。"""
from __future__ import annotations

import json
import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG_DIR = PROJECT_ROOT / "data" / "logs"
_configured = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "at": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name in ("event", "checkpoint_id", "status", "seconds", "requests", "tokens"):
            value = getattr(record, name, None)
            if value is not None:
                payload[name] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> Path:
    """配置 stderr 与轮转 JSON 文件日志，多次调用不会重复添加处理器。"""
    global _configured
    log_dir = Path(os.environ.get("LOG_DIR", "").strip() or DEFAULT_LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "app.jsonl"
    if _configured:
        return path
    level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    handler = RotatingFileHandler(
        path, maxBytes=int(os.environ.get("LOG_MAX_BYTES", "10485760")),
        backupCount=int(os.environ.get("LOG_BACKUP_COUNT", "5")), encoding="utf-8",
    )
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    _configured = True
    return path


def request_snapshot(llm) -> dict[str, int]:
    """汇总进程内近期模型请求计量；共享客户端下请求数是近似观测值。"""
    clients = getattr(llm, "clients", None)
    values = list(clients.values()) if clients else [llm]
    requests = success = tokens = 0
    for client in values:
        records = list(getattr(client, "requests", ()))
        requests += len(records)
        success += sum(bool(row.get("success")) for row in records)
        for row in records:
            usage = row.get("usage") or {}
            tokens += int(usage.get("total_tokens") or 0)
    return {"requests": requests, "success": success, "tokens": tokens}


def log_turn(logger: logging.Logger, checkpoint_id: str, status: str, started: float, before: dict, after: dict) -> None:
    seconds = round(time.monotonic() - started, 3)
    requests = max(0, after["requests"] - before["requests"])
    tokens = max(0, after["tokens"] - before["tokens"])
    extra = {
        "event": "creation_turn", "checkpoint_id": checkpoint_id,
        "status": status, "seconds": seconds, "requests": requests, "tokens": tokens,
    }
    logger.info("创作轮次完成", extra=extra)
    try:
        max_seconds = float(os.environ.get("ALERT_MAX_TURN_SECONDS", "300"))
        max_requests = int(os.environ.get("ALERT_MAX_REQUESTS", "30"))
        max_tokens = int(os.environ.get("ALERT_MAX_TOKENS", "100000"))
    except ValueError:
        max_seconds, max_requests, max_tokens = 300.0, 30, 100000
    if seconds > max_seconds or requests > max_requests or tokens > max_tokens:
        logger.warning("创作轮次超过运行阈值", extra={**extra, "event": "creation_alert"})

