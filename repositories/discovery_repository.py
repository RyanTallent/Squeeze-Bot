from __future__ import annotations

import json
from datetime import datetime
from threading import Lock
from typing import Any, Callable

from discovery_engine import Discovery


class DiscoveryRepository:
    def __init__(self, using_postgres: Callable[[], bool], pg_conn: Callable[[], Any], sqlite_conn: Callable[[], Any], db_lock: Lock):
        self.using_postgres = using_postgres
        self.pg_conn = pg_conn
        self.sqlite_conn = sqlite_conn
        self.db_lock = db_lock

    def save_discovery(self, user_id: str, discovery: Discovery) -> str:
        row = {
            "id": discovery.id,
            "user_id": user_id,
            "discovery_type": discovery.discovery_type,
            "title": discovery.title,
            "description": discovery.description,
            "confidence": discovery.confidence,
            "evidence_count": discovery.evidence_count,
            "source_module": discovery.source_module,
            "evidence_json": json.dumps(discovery.evidence, default=str),
            "status": "OPEN",
            "created_at_utc": discovery.created_at_utc,
            "updated_at_utc": datetime.utcnow().isoformat(),
        }
        keys = list(row.keys())
        if self.using_postgres():
            with self.pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"INSERT INTO discoveries ({', '.join(keys)}) VALUES ({', '.join('%(' + k + ')s' for k in keys)})",
                        row,
                    )
                conn.commit()
            return discovery.id

        with self.db_lock:
            conn = self.sqlite_conn()
            try:
                conn.execute(
                    f"INSERT INTO discoveries ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})",
                    tuple(row[k] for k in keys),
                )
                conn.commit()
            finally:
                conn.close()
        return discovery.id

    def list_discoveries(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM discoveries WHERE user_id=%s ORDER BY created_at_utc DESC LIMIT %s"
        if self.using_postgres():
            with self.pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, (user_id, limit))
                    rows = [dict(r) for r in cur.fetchall()]
        else:
            conn = self.sqlite_conn()
            try:
                cur = conn.cursor()
                cur.execute(sql.replace("%s", "?"), (user_id, limit))
                rows = [dict(r) for r in cur.fetchall()]
            finally:
                conn.close()
        for row in rows:
            try:
                row["evidence_json"] = json.loads(row.get("evidence_json") or "{}")
            except Exception:
                row["evidence_json"] = {}
        return rows
