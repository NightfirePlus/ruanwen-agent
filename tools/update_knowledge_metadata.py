"""为当前 catalog 补齐统一生命周期元数据；可重复运行。"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.catalogkb import DEFAULT_RUNTIME_KB, INDEX
from agent.knowledge_verification import lifecycle_metadata


def update(root: Path) -> dict:
    catalog_path = root / INDEX / "catalog.json"
    policy_path = root / INDEX / "runtime-policy.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    catalog_updated_at = catalog.get("updated_at") or datetime.now(
        timezone(timedelta(hours=8))
    ).isoformat(timespec="seconds")
    required = expiring = urls = 0
    for record in catalog["records"]:
        previous = record.get("lifecycle", {})
        generated = lifecycle_metadata(
            record, previous.get("ingested_at") or catalog_updated_at, policy
        )
        generated["last_verified_at"] = (
            previous.get("last_verified_at") or record.get("live_verified_at")
        )
        generated["expires_at"] = previous.get("expires_at")
        record["lifecycle"] = generated
        required += int(generated["verification_required"])
        expiring += int(generated["ttl_days"] is not None and generated["ttl_days"] > 0)
        urls += int(bool(generated["source_urls"]))
    catalog["metadata_schema"] = "knowledge-lifecycle-v1"
    catalog["official_source_check"] = (
        "Runtime official-text verification for freshness-required records; "
        "source_verified is not human publish approval"
    )
    catalog["metadata_updated_at"] = datetime.now(
        timezone(timedelta(hours=8))
    ).isoformat(timespec="seconds")
    catalog_path.write_bytes(
        (json.dumps(catalog, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    return {
        "records": len(catalog["records"]),
        "verification_required": required,
        "ttl_configured": expiring,
        "with_source_urls": urls,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kb", type=Path, default=DEFAULT_RUNTIME_KB)
    args = parser.parse_args()
    print(json.dumps(update(args.kb), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
