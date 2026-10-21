"""默认启用已确定的魔搭阶段分工，可显式选择单模型配置。"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .llm import LLMClient, LLMError, RequestBudget

CONFIG = Path(__file__).resolve().parents[1] / "config/model_routing.json"
STAGES = frozenset(("intake", "revise_intent", "evidence", "compose", "rewrite", "fact_review", "compliance_review"))


class ModelRouter:
    def __init__(self, clients: dict, stages: dict):
        if set(stages) != STAGES or any(role not in clients for role in stages.values()):
            raise LLMError("模型分工配置不完整，不能跳过必要阶段")
        self.clients = dict(clients)
        self.stages = dict(stages)

    def for_stage(self, stage: str):
        if stage not in self.stages:
            raise LLMError(f"未配置阶段模型：{stage}")
        return self.clients[self.stages[stage]]


def modelscope_router(*, budget: RequestBudget | None = None) -> ModelRouter:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    key = next((os.environ.get(k, "").strip() for k in (
        "MODELSCOPE_API_TOKEN", "MODELSCOPE_SDK_TOKEN", "MODELSCOPE_API_KEY", "LLM_API_KEY"
    ) if os.environ.get(k, "").strip()), "")
    if not key:
        raise LLMError("请设置 MODELSCOPE_API_TOKEN，或将魔搭访问令牌填入 LLM_API_KEY")
    clients = {
        role: LLMClient(api_key=key, base_url=config["base_url"], timeout=120, max_retries=0,
                        repair_json=False, budget=budget, **spec)
        for role, spec in config["models"].items()
    }
    return ModelRouter(clients, config["stages"])


def make_llm():
    profile = os.environ.get("LLM_PROFILE", "modelscope-v1").strip() or "modelscope-v1"
    if profile == "single":
        return LLMClient()
    if profile == "modelscope-v1":
        return modelscope_router()
    raise LLMError("LLM_PROFILE 必须为 single 或 modelscope-v1")
