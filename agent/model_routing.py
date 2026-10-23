"""按运行配置把各阶段路由到 LLM 端点的多模型分工，或单个兼容模型。"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .llm import LLMClient, LLMError, RequestBudget

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
LLM_MULTI_CONFIG = CONFIG_DIR / "llm_model_routing.json"
STAGES = frozenset((
    "intake", "revise_intent", "evidence",
    "compose", "rewrite",                         # 保留给兼容工具和旧运行记录
    "content_plan", "chunk_generate", "chunk_rewrite", "coherence",
    "chunk_review", "coherence_review", "fact_review", "compliance_review",
))


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


def llm_multi_router(*, budget: RequestBudget | None = None) -> ModelRouter:
    """使用 LLM_API_KEY 端点上的多模型分工，不回退到魔搭令牌或单模型。"""
    config = json.loads(LLM_MULTI_CONFIG.read_text(encoding="utf-8"))
    key = os.environ.get("LLM_API_KEY", "").strip()
    base_url = os.environ.get("LLM_BASE_URL", "").strip()
    if not key:
        raise LLMError("LLM 多模型分工需要设置 LLM_API_KEY")
    if not base_url:
        raise LLMError("LLM 多模型分工需要设置 LLM_BASE_URL")
    clients = {
        role: LLMClient(
            api_key=key,
            base_url=base_url,
            timeout=120,
            max_retries=2,
            repair_json=True,
            budget=budget,
            **spec,
        )
        for role, spec in config["models"].items()
    }
    return ModelRouter(clients, config["stages"])


def _optional_bool(name: str) -> bool | None:
    value = os.environ.get(name, "").strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return None


def make_llm():
    # 默认使用环境里的 LLM_API_KEY / LLM_BASE_URL 端点分工；未配置端点时退回单模型。
    profile = os.environ.get("LLM_PROFILE", "").strip()
    if not profile:
        profile = ("llm-multi-v1" if os.environ.get("LLM_API_KEY", "").strip()
                   and os.environ.get("LLM_BASE_URL", "").strip() else "single")
    if profile == "single":
        options = {}
        thinking = _optional_bool("LLM_ENABLE_THINKING")
        if thinking is not None:
            options["enable_thinking"] = thinking
        timeout = os.environ.get("LLM_TIMEOUT", "").strip()
        if timeout:
            options["timeout"] = float(timeout)
        return LLMClient(**options)
    if profile == "llm-multi-v1":
        return llm_multi_router()
    raise LLMError("LLM_PROFILE 必须为 single 或 llm-multi-v1")
