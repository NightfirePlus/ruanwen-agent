"""事实点提取:大模型只许摘录原文(提示词见 prompts/facts.md),代码再逐条核对摘录确实存在于资料中。"""
from __future__ import annotations

import re

from .models import Fact
from .prompts import Prompts


def squash(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def extract(
    llm, prompts: Prompts, model: str, title: str, media_id: str, content: str, start: int = 1
) -> tuple[list[Fact], list[str]]:
    """返回 (事实点, 资料内部冲突说明)。摘录不在原文中的条目会被丢弃。"""
    user = prompts.render("facts", {"型号": model, "资料标题": title, "资料全文": content})
    # 事实提取是逐字摘录任务,不使用软文写作的系统提示词(system.md)
    data = llm.chat_json(None, user)
    source = squash(content)
    facts: list[Fact] = []
    seen: set[str] = set()
    for item in data.get("facts", []):
        text = (item.get("text") if isinstance(item, dict) else str(item) or "").strip()
        key = squash(text)
        if not key or key in seen or key not in source:
            continue
        seen.add(key)
        facts.append(
            Fact(id=f"F{start + len(facts)}", model=model, text=text, source_title=title, media_id=media_id)
        )
    conflicts = [str(c) for c in data.get("conflicts", []) if c]
    return facts, conflicts
