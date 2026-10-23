"""查看历史核验记录、候选修订，执行显式批准的受控回写。核验不再在运行时执行。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.catalogkb import DEFAULT_RUNTIME_KB, INDEX, CatalogStore
from agent.envfile import load_dotenv
from agent.knowledge import KnowledgeError
from agent.knowledge_verification import VerificationStore, apply_candidate


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kb", type=Path, default=DEFAULT_RUNTIME_KB)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--list", action="store_true")
    group.add_argument("--candidates", action="store_true")
    group.add_argument("--apply", metavar="CANDIDATE_ID")
    group.add_argument("--reset-record", metavar="RECORD_ID")
    parser.add_argument("--approved-by", default="")
    parser.add_argument("--allow-sensitive", action="store_true")
    args = parser.parse_args()

    catalog = CatalogStore(args.kb)
    policy = catalog.policy()
    index_name = policy.get("freshness", {}).get(
        "verification_index", "knowledge-verification.sqlite3"
    )
    state = VerificationStore(args.kb / INDEX / index_name)
    if args.list:
        for row in state.rows():
            row["result"] = json.loads(row.pop("result_json"))
            print(json.dumps(row, ensure_ascii=False))
        return
    if args.candidates:
        for row in state.candidates():
            print(json.dumps(row, ensure_ascii=False))
        return
    if args.apply:
        path = apply_candidate(
            args.kb, state, args.apply, args.approved_by, args.allow_sensitive
        )
        if "01-产品事实" in path.as_posix():
            from tools.rebuild_knowledge_views import main as rebuild_views
            rebuild_views()
        print(json.dumps({"applied": args.apply, "path": str(path)}, ensure_ascii=False))
        return
    if args.reset_record:
        print("已清除非通过记录数：" + str(state.reset_record(args.reset_record)))
        return
    parser.print_help()


if __name__ == "__main__":
    try:
        main()
    except KnowledgeError as exc:
        print("核验停止：" + str(exc), file=sys.stderr)
        raise SystemExit(1)
