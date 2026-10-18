"""合规检查:词表与正则为主,不依赖大模型。"""
from __future__ import annotations

import re

from . import config
from .models import Draft, Fact, Issue


def _segments(draft: Draft) -> list[tuple[str, str]]:
    segs = [("标题", draft.title), ("导语", draft.intro), ("行动引导", draft.cta)]
    segs += [(f"正文第{i}段", p.text) for i, p in enumerate(draft.body, 1)]
    segs += [("标签", " ".join(draft.tags))]
    return [(w, t) for w, t in segs if t]


def check(draft: Draft, facts: list[Fact], user_extra: str = "") -> list[Issue]:
    rules = config.compliance_rules()
    fact_text = "".join(f.text for f in facts)
    issues: list[Issue] = []

    for where, text in _segments(draft):
        for key in ("absolute", "promise", "competitor", "experience"):
            rule = rules[key]
            for pat in rule["patterns"]:
                m = re.search(pat, text)
                if not m:
                    continue
                # 词出现在知识库事实原文中时,属于资料自带表述,不拦截
                if m.group(0) in fact_text:
                    continue
                issues.append(
                    Issue(
                        kind="compliance",
                        severity="error",
                        detail=f"{rule['label']}:“{m.group(0)}”",
                        where=where,
                        action=rule["suggestion"],
                    )
                )
                break

        # 价格与权益:只有事实点里出现同样的词才允许
        for token in rules["price_tokens"]:
            if token in text and token not in fact_text:
                issues.append(
                    Issue(
                        kind="compliance",
                        severity="error",
                        detail=f"{rules['price_label']}:“{token}”",
                        where=where,
                        action=rules["price_suggestion"],
                    )
                )
                break
    return issues
