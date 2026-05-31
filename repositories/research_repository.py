from __future__ import annotations

import json
from threading import Lock
from typing import Any, Callable


class ResearchRepository:
    def __init__(self, using_postgres: Callable[[], bool], pg_conn: Callable[[], Any], sqlite_conn: Callable[[], Any], db_lock: Lock):
        self.using_postgres = using_postgres
        self.pg_conn = pg_conn
        self.sqlite_conn = sqlite_conn
        self.db_lock = db_lock

    def save_report(self, user_id: str, report: dict[str, Any], profile: dict[str, Any] | None = None, ai: dict[str, Any] | None = None) -> str:
        row = {
            "id": report.get("id") or f"{user_id}-{report.get('ticker')}-{report.get('generated_at_utc')}",
            "user_id": user_id,
            "ticker": report.get("ticker"),
            "verdict": report.get("verdict"),
            "aggregate_score": report.get("aggregate_score"),
            "report_json": json.dumps(report, default=str),
            "profile_json": json.dumps(profile or {}, default=str),
            "ai_json": json.dumps(ai or {}, default=str),
            "created_at_utc": report.get("generated_at_utc"),
        }
        keys = list(row.keys())
        if self.using_postgres():
            with self.pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"INSERT INTO research_reports ({', '.join(keys)}) VALUES ({', '.join('%(' + k + ')s' for k in keys)})",
                        row,
                    )
                conn.commit()
            return row["id"]
        with self.db_lock:
            conn = self.sqlite_conn()
            try:
                conn.execute(
                    f"INSERT INTO research_reports ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})",
                    tuple(row[k] for k in keys),
                )
                conn.commit()
            finally:
                conn.close()
        return row["id"]

    def list_reports(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        sql = "SELECT * FROM research_reports WHERE user_id=%s ORDER BY created_at_utc DESC LIMIT %s"
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
            for key in ("report_json", "profile_json", "ai_json"):
                try:
                    row[key] = json.loads(row.get(key) or "{}")
                except Exception:
                    row[key] = {}
        return rows
