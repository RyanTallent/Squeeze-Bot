from __future__ import annotations

import json
from threading import Lock
from typing import Any, Callable


class BriefingRepository:
    def __init__(self, using_postgres: Callable[[], bool], pg_conn: Callable[[], Any], sqlite_conn: Callable[[], Any], db_lock: Lock):
        self.using_postgres = using_postgres
        self.pg_conn = pg_conn
        self.sqlite_conn = sqlite_conn
        self.db_lock = db_lock

    def save_briefing(self, user_id: str, briefing: dict[str, Any], source_context: dict[str, Any] | None = None) -> str:
        row = {
            "id": briefing["id"],
            "user_id": user_id,
            "briefing_type": briefing["briefing_type"],
            "priority": briefing.get("priority"),
            "title": briefing.get("title"),
            "summary": briefing.get("lead"),
            "content_json": json.dumps(briefing, default=str),
            "source_context_json": json.dumps(source_context or {}, default=str),
            "created_at_utc": briefing["generated_at_utc"],
        }
        keys = list(row.keys())
        if self.using_postgres():
            with self.pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"INSERT INTO briefing_runs ({', '.join(keys)}) VALUES ({', '.join('%(' + k + ')s' for k in keys)})",
                        row,
                    )
                conn.commit()
            return row["id"]

        with self.db_lock:
            conn = self.sqlite_conn()
            try:
                conn.execute(
                    f"INSERT INTO briefing_runs ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})",
                    tuple(row[k] for k in keys),
                )
                conn.commit()
            finally:
                conn.close()
        return row["id"]

    def list_briefings(self, user_id: str, briefing_type: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        where = ["user_id = %s"]
        params: list[Any] = [user_id]
        if briefing_type:
            where.append("briefing_type = %s")
            params.append(briefing_type)
        sql = f"SELECT * FROM briefing_runs WHERE {' AND '.join(where)} ORDER BY created_at_utc DESC LIMIT {int(limit)}"
        if self.using_postgres():
            with self.pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    rows = [dict(r) for r in cur.fetchall()]
        else:
            conn = self.sqlite_conn()
            try:
                cur = conn.cursor()
                cur.execute(sql.replace("%s", "?"), params)
                rows = [dict(r) for r in cur.fetchall()]
            finally:
                conn.close()
        for row in rows:
            try:
                row["content_json"] = json.loads(row.get("content_json") or "{}")
            except Exception:
                row["content_json"] = {}
            try:
                row["source_context_json"] = json.loads(row.get("source_context_json") or "{}")
            except Exception:
                row["source_context_json"] = {}
        return rows
