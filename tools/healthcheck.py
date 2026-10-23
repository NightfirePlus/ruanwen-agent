"""离线健康检查：配置、提示词、知识库、SQLite 和运行目录，不调用模型接口。"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.catalogkb import make_store
from agent.envfile import load_dotenv
from agent.model_routing import make_llm
from agent.prompts import Prompts
from agent.workspace import get_workspace


def check() -> dict:
    load_dotenv()
    checks, errors = {}, []
    try:
        Prompts.load(strict=True)
        checks["prompts"] = "ok"
    except Exception as exc:
        errors.append(f"prompts: {type(exc).__name__}: {exc}")
    try:
        store = make_store()
        connected = store.connect()
        checks["knowledge_base"] = {"connected": connected.get("connected", False), "records": len(store.records())}
        if not connected.get("connected"):
            errors.append("knowledge_base: disconnected")
        policy = store.policy()
        freshness = policy.get("freshness", {})
        required = [row for row in store.records() if row.get("lifecycle", {}).get("verification_required")]
        checks["freshness"] = {
            "enabled": bool(freshness.get("enabled")),
            "required_records": len(required),
            "verification_index": freshness.get("verification_index"),
        }
        if not freshness.get("enabled") or not freshness.get("verification_index"):
            errors.append("freshness: 官网时效门禁未完整启用")
    except Exception as exc:
        errors.append(f"knowledge_base: {type(exc).__name__}: {exc}")
    try:
        workspace = get_workspace()
        checks["workspace"] = {"path": str(workspace.path), "quick_check": workspace.quick_check()}
        if not checks["workspace"]["quick_check"]:
            errors.append("workspace: quick_check failed")
        with tempfile.NamedTemporaryFile(dir=workspace.path.parent, prefix="health-", delete=True):
            pass
        checks["data_writable"] = True
    except (OSError, sqlite3.Error) as exc:
        errors.append(f"workspace: {type(exc).__name__}: {exc}")
    try:
        llm = make_llm()
        checks["llm_config"] = getattr(llm, "model", "router")
    except Exception as exc:
        errors.append(f"llm_config: {type(exc).__name__}: {exc}")
    return {"passed": not errors, "checks": checks, "errors": errors}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    result = check()
    if not args.quiet or not result["passed"]:
        print(json.dumps(result, ensure_ascii=False, indent=None if args.quiet else 2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

