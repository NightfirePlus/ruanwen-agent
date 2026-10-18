"""阶段6:合规检查 = 规则 + LLM。

规则:词表与正则(绝对化、虚假承诺、竞品贬低、伪造体验、无依据价格权益)。
LLM:识别词表覆盖不到的隐性问题(暗示性对比、变相承诺、夸大效果)。
词出现在知识库事实原文里时,属于资料自带表述,规则不拦截。
"""
from __future__ import annotations

import re

from . import config
from .compose import draft_to_json, facts_block
from .models import Draft, Evidence, Issue
from .norm import llm_issues
from .prompts import Prompts, PromptError


def rule_check(draft: Draft, ev: Evidence) -> list[Issue]:
    rules = config.compliance_rules()
    fact_text = "".join(f.text for f in ev.facts)
    issues: list[Issue] = []

    for where, text in draft.segments():
        for key in ("absolute", "promise", "competitor", "experience"):
            rule = rules[key]
            for pat in rule["patterns"]:
                m = re.search(pat, text)
                if m and m.group(0) not in fact_text:
                    issues.append(Issue("compliance", "rule", "error", where, f"{rule['label']}:“{m.group(0)}”", rule["suggestion"]))
                    break

        hit = next((t for t in rules["price_tokens"] if t in text and t not in fact_text), None)
        if hit is None:
            for pat in rules.get("price_patterns", []):
                m = re.search(pat, text)
                if m and m.group(0) not in fact_text:
                    hit = m.group(0)
                    break
        if hit:
            issues.append(Issue("compliance", "rule", "error", where, f"{rules['price_label']}:“{hit}”", rules["price_suggestion"]))
    return issues


def llm_check(llm, prompts: Prompts, draft: Draft, ev: Evidence) -> list[Issue]:
    data = llm.json(prompts.render("compliance_review", {"稿件": draft_to_json(draft), "事实点清单": facts_block(ev)}))
    return llm_issues(data, "compliance")


def check(llm, prompts: Prompts, draft: Draft, ev: Evidence) -> list[Issue]:
    issues = rule_check(draft, ev)
    try:
        issues += llm_check(llm, prompts, draft, ev)
    except PromptError as e:
        issues.append(Issue("compliance", "llm", "warn", "合规检查", f"LLM 合规检查已跳过:{e}"))
    return issues
