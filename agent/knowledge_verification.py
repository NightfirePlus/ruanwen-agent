"""知识库核验记录与候选修订的读取、回写。

运行时不抓取官网、不调用核验模型；本模块只保留历史核验记录的存储与人工批准的受控回写。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .knowledge import KnowledgeError


SENSITIVE_RISKS = {"sku", "price", "benefit", "region", "legal", "conflict", "deletion", "high"}


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class VerificationStore:
    """保存时效核验和候选修订；状态与正式知识文件分离。"""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS verifications (
                    key TEXT PRIMARY KEY,
                    record_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    checked_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    result_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_verifications_record
                    ON verifications(record_id, checked_at DESC);
                CREATE TABLE IF NOT EXISTS revision_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    verification_key TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    record_path TEXT NOT NULL,
                    file_sha256 TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    risk TEXT NOT NULL,
                    status TEXT NOT NULL,
                    changes_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    applied_at TEXT,
                    approved_by TEXT
                );
            """)

    @contextmanager
    def db(self):
        try:
            db = sqlite3.connect(self.path, timeout=10)
            db.row_factory = sqlite3.Row
        except sqlite3.Error as exc:
            raise KnowledgeError("官网核验索引不可写") from exc
        try:
            with db:
                yield db
        except sqlite3.Error as exc:
            raise KnowledgeError("官网核验索引忙或损坏") from exc
        finally:
            db.close()

    def latest(self, record_id: str) -> dict | None:
        """该资料最近一次仍在有效期内的核验结论,不管当初用的什么提示词或模型。"""
        with self.db() as db:
            row = db.execute(
                "SELECT result_json,expires_at,status FROM verifications "
                "WHERE record_id=? ORDER BY checked_at DESC LIMIT 1", (record_id,)
            ).fetchone()
        if not row or _parse_time(row["expires_at"]) <= _utc_now() or row["status"] == "in_progress":
            return None
        return json.loads(row["result_json"])

    def reserve_or_get(self, key: str, record_id: str, retry_minutes: int = 10) -> dict | None:
        now = _utc_now()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM verifications WHERE key=?", (key,)).fetchone()
            if row and _parse_time(row["expires_at"]) > now:
                if row["status"] == "in_progress":
                    raise KnowledgeError("该资料的官网核验正在进行，未重复调用")
                return json.loads(row["result_json"])
            placeholder = {
                "status": "in_progress", "record_id": record_id,
                "checked_at": _iso(now), "expires_at": _iso(now + timedelta(minutes=retry_minutes)),
            }
            db.execute(
                "INSERT OR REPLACE INTO verifications VALUES (?,?,?,?,?,?)",
                (key, record_id, "in_progress", placeholder["checked_at"],
                 placeholder["expires_at"], json.dumps(placeholder, ensure_ascii=False)),
            )
        return None

    def save(self, key: str, record_id: str, result: dict, ttl_days: int, failure_hours: int = 6) -> dict:
        now = _utc_now()
        days = ttl_days if result.get("status") == "passed" else 0
        expires = now + (timedelta(days=days) if days else timedelta(hours=failure_hours))
        result = dict(result, checked_at=_iso(now), expires_at=_iso(expires))
        with self.db() as db:
            db.execute(
                "INSERT OR REPLACE INTO verifications VALUES (?,?,?,?,?,?)",
                (key, record_id, result["status"], result["checked_at"], result["expires_at"],
                 json.dumps(result, ensure_ascii=False)),
            )
        return result

    def add_candidates(self, key: str, record: dict, differences: list[dict]) -> list[str]:
        ids = []
        now = _iso(_utc_now())
        for change in differences:
            if not isinstance(change, dict) or not str(change.get("old", "")).strip():
                continue
            risk = str(change.get("risk") or "normal").lower()
            source_url = str(change.get("url") or "")
            candidate_id = "REV-" + _digest(_packed({
                "key": key, "record": record["id"], "change": change,
            }))[:16]
            with self.db() as db:
                db.execute(
                    "INSERT OR IGNORE INTO revision_candidates VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL)",
                    (candidate_id, key, record["id"], record["path"], record["sha256"],
                     source_url, risk, "pending", json.dumps([change], ensure_ascii=False), now),
                )
            ids.append(candidate_id)
        return ids

    def rows(self) -> list[dict]:
        with self.db() as db:
            return [dict(row) for row in db.execute(
                "SELECT key,record_id,status,checked_at,expires_at,result_json FROM verifications ORDER BY checked_at DESC"
            )]

    def reset_record(self, record_id: str) -> int:
        """显式清除某条资料的非通过核验，供修复配置后重试。"""
        with self.db() as db:
            return db.execute(
                "DELETE FROM verifications WHERE record_id=? AND status!='passed'", (record_id,)
            ).rowcount

    def candidates(self, status: str | None = None) -> list[dict]:
        query = "SELECT * FROM revision_candidates"
        args: tuple = ()
        if status:
            query += " WHERE status=?"
            args = (status,)
        query += " ORDER BY created_at DESC"
        with self.db() as db:
            rows = []
            for row in db.execute(query, args):
                item = dict(row)
                item["changes"] = json.loads(item.pop("changes_json"))
                rows.append(item)
            return rows

    def mark_applied(self, candidate_id: str, approved_by: str) -> None:
        with self.db() as db:
            changed = db.execute(
                "UPDATE revision_candidates SET status='applied',applied_at=?,approved_by=? "
                "WHERE candidate_id=? AND status='pending'",
                (_iso(_utc_now()), approved_by, candidate_id),
            ).rowcount
            if changed != 1:
                raise KnowledgeError("候选修订不存在或已经处理")


def lifecycle_metadata(record: dict, catalog_updated_at: str, policy: dict) -> dict:
    """生成可持久化到 catalog 的资料生命周期元数据。"""
    cfg = policy.get("freshness", {})
    ttl = cfg.get("ttl_days_by_kind", {}).get(record.get("kind"))
    required = (
        bool(cfg.get("enabled"))
        and record.get("id") not in set(cfg.get("verification_exempt_record_ids", []))
        and record.get("kind") in set(cfg.get("required_kinds", []))
    )
    return {
        "material_type": record.get("kind", ""),
        "ingested_at": catalog_updated_at,
        "source_urls": list(record.get("source", {}).get("urls", [])),
        "source_dates": list(record.get("source", {}).get("dates_in_source", [])),
        "verification_required": required,
        "ttl_days": int(ttl) if ttl is not None else None,
        "last_verified_at": record.get("live_verified_at"),
        "expires_at": None,
    }


def apply_candidate(
    root: Path,
    verification_store: VerificationStore,
    candidate_id: str,
    approved_by: str,
    allow_sensitive: bool = False,
) -> Path:
    """在显式批准后应用精确替换，并同步 catalog 哈希；不接受模糊改写。"""
    if not approved_by.strip():
        raise KnowledgeError("受控回写必须记录批准人")
    matches = [row for row in verification_store.candidates("pending") if row["candidate_id"] == candidate_id]
    if not matches:
        raise KnowledgeError("候选修订不存在或已经处理")
    candidate = matches[0]
    if candidate["risk"] in SENSITIVE_RISKS and not allow_sensitive:
        raise KnowledgeError("敏感候选修订需要显式 allow_sensitive 人工批准")
    path = (root / candidate["record_path"]).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise KnowledgeError("候选修订目标路径无效")
    raw = path.read_bytes()
    if _digest(raw) != candidate["file_sha256"]:
        raise KnowledgeError("正式资料已变化，候选修订失效")
    text = raw.decode("utf-8-sig")
    for change in candidate["changes"]:
        old, new = str(change.get("old", "")), str(change.get("new", ""))
        if not old or not new or text.count(old) != 1:
            raise KnowledgeError("候选修订不是唯一精确替换，已停止")
        text = text.replace(old, new, 1)
    catalog_path = root / "00-规范与索引" / "catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    record = next((row for row in catalog["records"] if row["id"] == candidate["record_id"]), None)
    if not record or record["path"] != candidate["record_path"]:
        raise KnowledgeError("候选修订与 catalog 不一致")
    path.write_bytes(text.replace("\r\n", "\n").encode("utf-8"))
    record["sha256"] = _digest(path.read_bytes())
    record["review_status"] = "pending_review"
    record["publishable"] = False
    record["live_verified_at"] = None
    if "lifecycle" in record:
        record["lifecycle"]["last_verified_at"] = None
        record["lifecycle"]["expires_at"] = None
    catalog["updated_at"] = _iso(_utc_now())
    catalog_path.write_bytes((json.dumps(catalog, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    verification_store.mark_applied(candidate_id, approved_by.strip())
    return path
