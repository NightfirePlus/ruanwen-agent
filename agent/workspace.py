"""账户下的历史作品、宣传数据与用户偏好持久化。

创作过程仍保存在 Session 中；只有用户明确确认定稿后，才调用
``save_confirmed_work`` 写入历史作品库。宣传数据按作品 ID 追加记录，
用户偏好使用独立的数据表，避免与单次对话状态混在一起。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import export
from .models import Session


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKSPACE_DB = PROJECT_ROOT / "data" / "marketing_workspace.sqlite3"
SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_ACCOUNT_ID = "local"


def _now() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


class WorkspaceStore:
    """线程安全的轻量 SQLite 存储；每次操作使用独立连接。"""

    def __init__(self, path: Path | str = DEFAULT_WORKSPACE_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS works (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content_hash TEXT NOT NULL UNIQUE,
                    confirmed_at TEXT NOT NULL,
                    product TEXT NOT NULL,
                    audience TEXT NOT NULL,
                    scene TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    title TEXT NOT NULL,
                    article_markdown TEXT NOT NULL,
                    session_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_works_confirmed_at
                    ON works(confirmed_at DESC);

                CREATE TABLE IF NOT EXISTS performance_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
                    recorded_at TEXT NOT NULL,
                    impressions INTEGER NOT NULL DEFAULT 0,
                    likes INTEGER NOT NULL DEFAULT 0,
                    shares INTEGER NOT NULL DEFAULT 0,
                    inquiries INTEGER NOT NULL DEFAULT 0,
                    store_visits INTEGER NOT NULL DEFAULT 0,
                    orders INTEGER NOT NULL DEFAULT 0,
                    notes TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_performance_work_id
                    ON performance_records(work_id, recorded_at DESC);

                CREATE TABLE IF NOT EXISTS store_memories (
                    store_name TEXT PRIMARY KEY,
                    preferred_tone TEXT NOT NULL DEFAULT '',
                    preferred_platforms TEXT NOT NULL DEFAULT '',
                    audience_focus TEXT NOT NULL DEFAULT '',
                    content_preferences TEXT NOT NULL DEFAULT '',
                    avoid_topics TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_preferences (
                    account_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL DEFAULT '当前用户',
                    preferred_tone TEXT NOT NULL DEFAULT '',
                    preferred_platforms TEXT NOT NULL DEFAULT '',
                    audience_focus TEXT NOT NULL DEFAULT '',
                    content_preferences TEXT NOT NULL DEFAULT '',
                    avoid_topics TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                """
            )
            # 兼容已经由上一版本创建的作品库。
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(works)")}
            if "updated_at" not in columns:
                conn.execute("ALTER TABLE works ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
            # 旧版门店记忆只迁移一次到当前账户，保留原表便于回滚。
            exists = conn.execute(
                "SELECT 1 FROM user_preferences WHERE account_id = ?", (DEFAULT_ACCOUNT_ID,)
            ).fetchone()
            if not exists:
                legacy = conn.execute(
                    "SELECT * FROM store_memories ORDER BY updated_at DESC LIMIT 1"
                ).fetchone()
                if legacy:
                    conn.execute(
                        """
                        INSERT INTO user_preferences(
                            account_id, display_name, preferred_tone, preferred_platforms,
                            audience_focus, content_preferences, avoid_topics, notes, updated_at
                        ) VALUES (?, '当前用户', ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            DEFAULT_ACCOUNT_ID, legacy["preferred_tone"],
                            legacy["preferred_platforms"], legacy["audience_focus"],
                            legacy["content_preferences"], legacy["avoid_topics"],
                            legacy["notes"], _now(),
                        ),
                    )

    @staticmethod
    def _work_hash(session: Session) -> str:
        payload = {
            "brief": asdict(session.brief),
            "draft": asdict(session.draft) if session.draft else None,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def save_confirmed_work(self, session: Session, image_url=None) -> int:
        """保存已确认且通过核验的成品；相同内容重复确认不会产生重复作品。"""
        if not session.draft or not session.review or not session.review.passed:
            raise ValueError("只有已生成且通过核验的稿件才能保存为历史作品")
        digest = self._work_hash(session)
        b = session.brief
        with self._connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO works(
                    content_hash, confirmed_at, product, audience, scene, platform,
                    goal, title, article_markdown, session_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    digest, _now(), b.product, b.audience, b.scene, b.platform,
                    b.goal, session.draft.title,
                    export.to_article_markdown(session, image_url=image_url),
                    json.dumps(session.to_dict(), ensure_ascii=False), _now(),
                ),
            )
            row = conn.execute("SELECT id FROM works WHERE content_hash = ?", (digest,)).fetchone()
        return int(row["id"])

    def list_works(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT id, confirmed_at, product, audience, scene, platform, goal, title
                FROM works
                ORDER BY COALESCE(NULLIF(updated_at, ''), confirmed_at) DESC, id DESC LIMIT ?
                """,
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_work(self, work_id: int | str) -> dict[str, Any] | None:
        try:
            key = int(work_id)
        except (TypeError, ValueError):
            return None
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM works WHERE id = ?", (key,)).fetchone()
        return dict(row) if row else None

    def update_work(
        self,
        work_id: int | str,
        *,
        title: str,
        product: str,
        audience: str,
        scene: str,
        platform: str,
        goal: str,
        article_markdown: str,
    ) -> dict[str, Any]:
        """修改账户下的历史作品，同时保留最初生成时的 session_json 审计记录。"""
        key = int(work_id)
        values = {
            "title": (title or "").strip(), "product": (product or "").strip(),
            "audience": (audience or "").strip(), "scene": (scene or "").strip(),
            "platform": (platform or "").strip(), "goal": (goal or "").strip(),
            "article_markdown": (article_markdown or "").strip(),
        }
        if not values["title"] or not values["article_markdown"]:
            raise ValueError("标题和作品正文不能为空")
        digest_raw = json.dumps({"id": key, **values}, ensure_ascii=False, sort_keys=True).encode("utf-8")
        digest = "edited:" + hashlib.sha256(digest_raw).hexdigest()
        with self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE works SET
                    content_hash=?, title=?, product=?, audience=?, scene=?, platform=?,
                    goal=?, article_markdown=?, updated_at=?
                WHERE id=?
                """,
                (
                    digest, values["title"], values["product"], values["audience"],
                    values["scene"], values["platform"], values["goal"],
                    values["article_markdown"], _now(), key,
                ),
            )
            if not cursor.rowcount:
                raise ValueError("未找到对应的历史作品")
        return self.get_work(key) or {}

    def add_performance(
        self,
        work_id: int | str,
        impressions: int = 0,
        likes: int = 0,
        shares: int = 0,
        inquiries: int = 0,
        store_visits: int = 0,
        orders: int = 0,
        notes: str = "",
    ) -> int:
        values = [impressions, likes, shares, inquiries, store_visits, orders]
        metrics = [max(0, int(value or 0)) for value in values]
        key = int(work_id)
        with self._connection() as conn:
            exists = conn.execute("SELECT 1 FROM works WHERE id = ?", (key,)).fetchone()
            if not exists:
                raise ValueError("未找到对应的历史作品")
            cursor = conn.execute(
                """
                INSERT INTO performance_records(
                    work_id, recorded_at, impressions, likes, shares,
                    inquiries, store_visits, orders, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (key, _now(), *metrics, (notes or "").strip()),
            )
        return int(cursor.lastrowid)

    def performance_for(self, work_id: int | str) -> list[dict[str, Any]]:
        try:
            key = int(work_id)
        except (TypeError, ValueError):
            return []
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM performance_records
                WHERE work_id = ? ORDER BY recorded_at DESC, id DESC
                """,
                (key,),
            ).fetchall()
        return [dict(row) for row in rows]

    def performance_guidance(self, brief, limit: int = 3) -> dict[str, Any]:
        """把历史宣传数据转换为策略参考权重，不把历史正文当作产品事实。

        权重同时考虑效果率、数据量可信度和与本次任务的相似度。曝光量只提高
        可信度，不会单独让低互动作品获得高权重。
        """
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT w.id, w.title, w.product, w.audience, w.scene, w.platform,
                       w.goal, w.article_markdown,
                       COUNT(p.id) AS record_count,
                       SUM(p.impressions) AS impressions,
                       SUM(p.likes) AS likes,
                       SUM(p.shares) AS shares,
                       SUM(p.inquiries) AS inquiries,
                       SUM(p.store_visits) AS store_visits,
                       SUM(p.orders) AS orders
                FROM works w JOIN performance_records p ON p.work_id = w.id
                GROUP BY w.id
                """
            ).fetchall()

        candidates = []
        for row in rows:
            data = dict(row)
            similarity_parts = {
                "同平台": 4 if data["platform"] == brief.platform else 0,
                "同场景": 3 if data["scene"] == brief.scene else 0,
                "同受众": 2 if data["audience"] == brief.audience else 0,
                "同目标": 2 if data["goal"] == brief.goal else 0,
                "同产品": 1 if data["product"] == brief.product else 0,
            }
            similarity = sum(similarity_parts.values())
            if similarity == 0:
                continue

            impressions = int(data["impressions"] or 0)
            weighted_actions = (
                int(data["likes"] or 0)
                + 2 * int(data["shares"] or 0)
                + 4 * int(data["inquiries"] or 0)
                + 6 * int(data["store_visits"] or 0)
                + 10 * int(data["orders"] or 0)
            )
            if impressions > 0:
                effectiveness = min(1.0, weighted_actions / impressions / 0.12)
                confidence = min(1.0, math.log10(impressions + 1) / 3.0)
            else:
                effectiveness = min(1.0, weighted_actions / 20.0)
                confidence = min(0.5, 0.12 * int(data["record_count"] or 0))
            similarity_factor = 0.55 + 0.45 * (similarity / 12.0)
            weight = round((0.72 * effectiveness + 0.28 * confidence) * similarity_factor, 3)
            matched = [name for name, points in similarity_parts.items() if points]
            candidates.append(
                {
                    "work_id": data["id"],
                    "title": data["title"],
                    "match": matched,
                    "reference_weight": weight,
                    "performance": {
                        key: int(data[key] or 0)
                        for key in (
                            "impressions", "likes", "shares", "inquiries",
                            "store_visits", "orders", "record_count",
                        )
                    },
                    "content_excerpt": (data["article_markdown"] or "")[:800],
                }
            )
        candidates.sort(key=lambda item: (item["reference_weight"], len(item["match"])), reverse=True)
        selected = candidates[: max(1, min(int(limit), 5))]
        return {
            "mode": "advisory_weight",
            "rule": "权重越高越优先参考表达方式与内容角度；不得作为产品事实、参数或承诺依据",
            "references": selected,
        }

    def save_store_memory(self, store_name: str, **fields: str) -> dict[str, Any]:
        name = (store_name or "").strip()
        if not name:
            raise ValueError("请填写门店名称")
        allowed = (
            "preferred_tone", "preferred_platforms", "audience_focus",
            "content_preferences", "avoid_topics", "notes",
        )
        values = [(fields.get(field) or "").strip() for field in allowed]
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO store_memories(
                    store_name, preferred_tone, preferred_platforms, audience_focus,
                    content_preferences, avoid_topics, notes, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(store_name) DO UPDATE SET
                    preferred_tone=excluded.preferred_tone,
                    preferred_platforms=excluded.preferred_platforms,
                    audience_focus=excluded.audience_focus,
                    content_preferences=excluded.content_preferences,
                    avoid_topics=excluded.avoid_topics,
                    notes=excluded.notes,
                    updated_at=excluded.updated_at
                """,
                (name, *values, _now()),
            )
        return self.get_store_memory(name) or {}

    def get_store_memory(self, store_name: str) -> dict[str, Any] | None:
        name = (store_name or "").strip()
        if not name:
            return None
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM store_memories WHERE store_name = ?", (name,)
            ).fetchone()
        return dict(row) if row else None

    def list_store_memories(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM store_memories ORDER BY updated_at DESC, store_name"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_user_preferences(self, account_id: str = DEFAULT_ACCOUNT_ID) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM user_preferences WHERE account_id = ?", (account_id,)
            ).fetchone()
        if row:
            return dict(row)
        return {
            "account_id": account_id, "display_name": "当前用户",
            "preferred_tone": "", "preferred_platforms": "", "audience_focus": "",
            "content_preferences": "", "avoid_topics": "", "notes": "", "updated_at": "",
        }

    def save_user_preferences(
        self, account_id: str = DEFAULT_ACCOUNT_ID, **fields: str
    ) -> dict[str, Any]:
        allowed = (
            "display_name", "preferred_tone", "preferred_platforms", "audience_focus",
            "content_preferences", "avoid_topics", "notes",
        )
        cleaned = {field: (fields.get(field) or "").strip() for field in allowed}
        cleaned["display_name"] = cleaned["display_name"] or "当前用户"
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO user_preferences(
                    account_id, display_name, preferred_tone, preferred_platforms,
                    audience_focus, content_preferences, avoid_topics, notes, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    display_name=excluded.display_name,
                    preferred_tone=excluded.preferred_tone,
                    preferred_platforms=excluded.preferred_platforms,
                    audience_focus=excluded.audience_focus,
                    content_preferences=excluded.content_preferences,
                    avoid_topics=excluded.avoid_topics,
                    notes=excluded.notes,
                    updated_at=excluded.updated_at
                """,
                (
                    account_id, cleaned["display_name"], cleaned["preferred_tone"],
                    cleaned["preferred_platforms"], cleaned["audience_focus"],
                    cleaned["content_preferences"], cleaned["avoid_topics"],
                    cleaned["notes"], _now(),
                ),
            )
        return self.get_user_preferences(account_id)


@lru_cache(maxsize=1)
def get_workspace() -> WorkspaceStore:
    configured = os.environ.get("WORKSPACE_DB", "").strip()
    return WorkspaceStore(Path(configured) if configured else DEFAULT_WORKSPACE_DB)
