from __future__ import annotations

import json
import uuid
from datetime import datetime
from threading import Lock
from typing import Any, Callable


class PlaybookRepository:
    def __init__(self, using_postgres: Callable[[], bool], pg_conn: Callable[[], Any], sqlite_conn: Callable[[], Any], db_lock: Lock):
        self.using_postgres = using_postgres
        self.pg_conn = pg_conn
        self.sqlite_conn = sqlite_conn
        self.db_lock = db_lock

    def save_snapshot(self, user_id: str, stats: dict[str, Any]) -> str:
        snapshot_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()
        row = {
            "id": snapshot_id,
            "user_id": user_id,
            "sample_size": stats.get("sample_size") or 0,
            "win_rate": stats.get("win_rate"),
            "expectancy": stats.get("expectancy"),
            "snapshot_json": json.dumps(stats, default=str),
            "created_at_utc": now,
        }
        keys = list(row.keys())
        if self.using_postgres():
            with self.pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"INSERT INTO playbook_snapshots ({', '.join(keys)}) VALUES ({', '.join('%(' + k + ')s' for k in keys)})",
                        row,
                    )
                conn.commit()
            return snapshot_id

        with self.db_lock:
            conn = self.sqlite_conn()
            try:
                conn.execute(
                    f"INSERT INTO playbook_snapshots ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})",
                    tuple(row[k] for k in keys),
                )
                conn.commit()
            finally:
                conn.close()
        return snapshot_id

    def latest_snapshot(self, user_id: str) -> dict[str, Any] | None:
        sql = "SELECT * FROM playbook_snapshots WHERE user_id=%s ORDER BY created_at_utc DESC LIMIT 1"
        if self.using_postgres():
            with self.pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, (user_id,))
                    row = cur.fetchone()
                    out = dict(row) if row else None
        else:
            conn = self.sqlite_conn()
            try:
                cur = conn.cursor()
                cur.execute(sql.replace("%s", "?"), (user_id,))
                row = cur.fetchone()
                out = dict(row) if row else None
            finally:
                conn.close()
        if not out:
            return None
        try:
            out["snapshot_json"] = json.loads(out.get("snapshot_json") or "{}")
        except Exception:
            out["snapshot_json"] = {}
        return out
