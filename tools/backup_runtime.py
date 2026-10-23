"""一致性备份工作台数据库和知识审核缓存；不复制 .env 或任何凭证。"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.catalogkb import DEFAULT_RUNTIME_KB, INDEX
from agent.workspace import get_workspace


def backup_sqlite(source: Path, target: Path) -> Path:
    source = source.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    os.close(handle)
    temp = Path(temp_name)
    try:
        with closing(sqlite3.connect(source)) as current, closing(sqlite3.connect(temp)) as copy:
            current.backup(copy)
            if copy.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError(f"备份完整性检查失败：{source}")
            copy.commit()
        temp.replace(target)
        return target
    finally:
        temp.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path,
        default=Path(os.environ.get("BACKUP_DIR", "").strip() or ROOT / "data" / "backups"),
        help="备份根目录；每次运行会在其中创建带时间戳的子目录",
    )
    args = parser.parse_args()
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S")
    folder = args.output.resolve() / stamp
    folder.mkdir(parents=True, exist_ok=False)

    workspace = get_workspace().path
    verification = DEFAULT_RUNTIME_KB / INDEX / "knowledge-verification.sqlite3"
    files = []
    for source, name in (
        (workspace, "marketing_workspace.sqlite3"),
        (verification, "knowledge-verification.sqlite3"),
    ):
        if source.is_file():
            target = backup_sqlite(source, folder / name)
            files.append({"source": str(source.resolve()), "backup": str(target), "bytes": target.stat().st_size})
    manifest = {"created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"), "files": files}
    (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": bool(files), "folder": str(folder), "files": len(files)}, ensure_ascii=False))
    return 0 if files else 1


if __name__ == "__main__":
    raise SystemExit(main())

