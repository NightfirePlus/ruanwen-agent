"""事实点提取:大模型只许摘录原文,代码再逐条核对摘录确实存在于资料中。"""
from __future__ import annotations

import re

from .models import Fact

FACT_PROMPT = """从下面的产品资料中摘录可用于营销的事实点。输出JSON对象:{"facts":[{"text":"..."}]}。
要求:
1. text 必须是资料中的原文片段,逐字摘录,不改写、不合并、不换算、不补充。
2. 每条只含一个事实(一个参数、一项功能或一个特点)。
3. 资料中没有的内容不要出现。
4. 如发现资料内部对同一参数有互相矛盾的写法,在 conflicts 数组里说明,例如 "conflicts":["重量一处写1.4kg,另一处写1.5kg"];没有则为空数组。
只输出JSON。"""


def squash(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def extract(llm, model: str, title: str, media_id: str, content: str, start: int = 1) -> tuple[list[Fact], list[str]]:
    """返回 (事实点, 资料内部冲突说明)。摘录不在原文中的条目会被丢弃。"""
    data = llm.chat_json(FACT_PROMPT, f"型号:{model}\n资料标题:{title}\n\n资料全文:\n{content}")
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
