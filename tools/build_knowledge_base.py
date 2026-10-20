"""构建或只读检查精简本地知识库。"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.kb_migration import MigrationError, build_snapshot, validate_snapshot
from agent.knowledge import KnowledgeError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="外部源库路径")
    parser.add_argument("--output", type=Path, required=True, help="明确指定提取目标；不覆盖现有库")
    parser.add_argument("--policy", type=Path, default=ROOT / "config/kb_selection.json")
    parser.add_argument("--validate", action="store_true", help="只读校验已有快照")
    args = parser.parse_args()
    try:
        if args.validate:
            result = validate_snapshot(args.source, args.output)
        else:
            policy = json.loads(args.policy.read_text(encoding="utf-8"))
            result = build_snapshot(args.source, args.output, policy)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["structural_checks_passed"] else 1
    except (MigrationError, OSError, ValueError, KnowledgeError) as exc:
        print(f"知识库整理失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
