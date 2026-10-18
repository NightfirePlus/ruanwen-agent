"""策略表加载。场景、平台、合规规则全部来自 config/*.json,不写在代码里。"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@lru_cache(maxsize=None)
def _load(name: str) -> dict:
    return json.loads((ROOT / "config" / name).read_text(encoding="utf-8"))


def scenes() -> dict:
    return _load("scenes.json")


def platforms() -> dict:
    return _load("platforms.json")


def compliance_rules() -> dict:
    return _load("compliance_rules.json")


def match_alias(text: str, table: dict) -> str | None:
    """在策略表中按名称或别名匹配,返回规范名;匹配不到返回 None。"""
    text = (text or "").strip()
    if not text:
        return None
    if text in table:
        return text
    for name, cfg in table.items():
        for alias in cfg.get("aliases", []):
            if alias and (alias in text or text in alias):
                return name
    return None
