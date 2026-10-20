"""查看使用审核标记，或显式清除某条失败/不确定记录以便下次重审。"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.catalogkb import DEFAULT_RUNTIME_KB, INDEX, ReviewCache


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kb", type=Path, default=DEFAULT_RUNTIME_KB)
    parser.add_argument("--reset", help="精确指定缓存key；只清除非通过记录，不修改原文")
    args = parser.parse_args()
    path = args.kb / INDEX / "usage-review.sqlite3"
    if not path.exists():
        print("没有使用审核记录")
        return
    cache = ReviewCache(path)
    with cache.db() as db:
        if args.reset:
            cursor = db.execute("DELETE FROM reviews WHERE key=? AND status IN ('failed','in_progress','uncertain','rejected')", (args.reset,))
            print("已清除记录数：" + str(cursor.rowcount) + "；未发起模型调用")
        else:
            for key, status, raw, at in db.execute("SELECT key,status,result,at FROM reviews ORDER BY at DESC"):
                result = json.loads(raw) if raw else {}
                print(json.dumps(dict(key=key, status=status, at=at, metadata=result.get('_review'), reason=result.get('reason')), ensure_ascii=False))


if __name__ == "__main__":
    main()
