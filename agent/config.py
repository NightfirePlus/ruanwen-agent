"""配置与提示词加载。策略规则全部来自 config/*.json,不写死在代码里。"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@lru_cache(maxsize=None)
def load_json(name: str) -> dict:
    return json.loads((ROOT / "config" / name).read_text(encoding="utf-8"))


def scenes() -> dict:
    return load_json("scenes.json")


def platforms() -> dict:
    return load_json("platforms.json")


def compliance_rules() -> dict:
    return load_json("compliance_rules.json")


def system_prompt() -> str:
    return (ROOT / "prompts" / "system.md").read_text(encoding="utf-8")


def match_alias(text: str, table: dict) -> str | None:
    """在配置表中按名称/别名匹配,返回规范名;匹配不到返回 None。"""
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
