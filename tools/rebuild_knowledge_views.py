"""从当前产品事实重建派生横向视图；不依赖任何旧库。"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CURRENT = ROOT / "knowledge_base" / "current"
INDEX = CURRENT / "00-规范与索引"
CATALOG = INDEX / "catalog.json"
OUTPUT = INDEX / "派生视图" / "产品横向检索.json"
HEADING = re.compile(r"^##\s+(.+?)\s*$")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sections(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    name: str | None = None
    lines: list[str] = []
    for line in text.splitlines():
        match = HEADING.match(line)
        if match:
            if name is not None:
                result[name] = "\n".join(lines).strip()
            name, lines = match.group(1), []
        elif name is not None:
            lines.append(line)
    if name is not None:
        result[name] = "\n".join(lines).strip()
    return result


def main() -> None:
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    products = []
    for record in catalog["records"]:
        if record.get("kind") != "product_facts":
            continue
        path = (CURRENT / record["path"]).resolve()
        if not path.is_relative_to(CURRENT.resolve()) or not path.is_file():
            raise SystemExit(f"产品事实路径无效：{record['path']}")
        raw = path.read_bytes()
        actual = digest(raw)
        if actual != record["sha256"]:
            raise SystemExit(f"catalog 哈希尚未同步：{record['path']}")
        products.append({
            "model_key": record["model_key"],
            "official_product": record["official_product"],
            "record_id": record["id"],
            "source_path": record["path"],
            "source_sha256": actual,
            "sections": sections(raw.decode("utf-8-sig")),
        })
    view = {
        "version": 1,
        "generated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "authority": "派生检索视图，不是独立事实源；冲突时以source_path对应产品事实为准",
        "products": sorted(products, key=lambda row: row["model_key"]),
    }
    OUTPUT.write_bytes((json.dumps(view, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    print(json.dumps({"products": len(products), "output": str(OUTPUT)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
