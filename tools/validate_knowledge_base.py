"""独立校验当前知识库；不需要旧库，不调用模型，不另存历史报告。"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PIL import Image
from agent.catalogkb import CatalogStore, DEFAULT_RUNTIME_KB, INDEX, sha
from agent.kb_migration import LINK


def inspect_library(root):
    store = CatalogStore(root)
    errors, images = [], 0
    try:
        records = store.records()
        store.policy()
        paths = [r["path"] for r in records]
        if len(paths) != len(set(paths)):
            errors.append("索引包含重复文件路径")
        for record in records:
            try:
                raw = store._raw(record)
                path = store._safe(record["path"])
                if record["category"] == "images":
                    with Image.open(path) as picture:
                        if record.get("width", picture.width) != picture.width or record.get("height", picture.height) != picture.height:
                            errors.append("图片尺寸与索引不一致：" + record["path"])
                        picture.verify()
                    images += 1
                if path.suffix.lower() == ".md":
                    for match in LINK.finditer(raw.decode("utf-8-sig")):
                        target = match[2].strip().strip("<>")
                        parts = urlsplit(target)
                        if parts.scheme and parts.scheme != "file" or parts.netloc or target.startswith("#"):
                            continue
                        linked = (path.parent / unquote(parts.path)).resolve()
                        if not linked.is_relative_to(store.root) or not linked.exists():
                            errors.append("内部链接越出当前库或缺失：" + record["path"])
            except (OSError, ValueError, KeyError) as exc:
                errors.append(f"{record.get('path')}：{type(exc).__name__}")
            except Exception as exc:
                from agent.knowledge import KnowledgeError
                if not isinstance(exc, KnowledgeError):
                    raise
                errors.append(str(exc))
        source_index = store._safe(f"{INDEX}/source-registers.json")
        if source_index.exists():
            for row in json.loads(source_index.read_text(encoding="utf-8")):
                if sha(store._safe(row["path"]).read_bytes()) != row["sha256"]:
                    errors.append("来源登记表哈希不一致：" + row["path"])
        cache = store._safe(f"{INDEX}/usage-review.sqlite3")
        if cache.exists():
            with sqlite3.connect(cache.as_uri() + "?mode=ro", uri=True) as db:
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    errors.append("审核索引损坏")
    except Exception as exc:
        from agent.knowledge import KnowledgeError
        if not isinstance(exc, (KnowledgeError, OSError, ValueError, KeyError, sqlite3.Error)):
            raise
        errors.append(str(exc))
        records = []
    return dict(passed=not errors, records=len(records), images=images, errors=errors)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kb", type=Path, default=DEFAULT_RUNTIME_KB)
    result = inspect_library(parser.parse_args().kb)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["passed"] else 1)
