from __future__ import annotations

import json
from threading import Lock
from typing import Any, Callable


class CommitteeRepository:
    def __init__(self, using_postgres: Callable[[], bool], pg_conn: Callable[[], Any], sqlite_conn: Callable[[], Any], db_lock: Lock):
        self.using_postgres = using_postgres
        self.pg_conn = pg_conn
        self.sqlite_conn = sqlite_conn
        self.db_lock = db_lock

    def save_run(self, user_id: str, committee: dict[str, Any], source_context: dict[str, Any] | None = None) -> str:
        row = {
            "id": committee.get("id") or committee.get("created_at_utc", "").replace(":", "-") + "-" + user_id,
            "user_id": user_id,
            "committee_type": committee.get("committee_type") or "general",
            "consensus": (committee.get("synthesis") or {}).get("consensus"),
            "final_recommendation": (committee.get("synthesis") or {}).get("final_recommendation"),
            "confidence": (committee.get("synthesis") or {}).get("confidence"),
            "votes_json": json.dumps(committee.get("votes") or [], default=str),
            "evidence_json": json.dumps(source_context or {}, default=str),
            "synthesis_json": json.dumps(committee.get("synthesis") or {}, default=str),
            "created_at_utc": committee.get("created_at_utc"),
        }
        keys = list(row.keys())
        if self.using_postgres():
            with self.pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"INSERT INTO committee_runs ({', '.join(keys)}) VALUES ({', '.join('%(' + k + ')s' for k in keys)})",
                        row,
                    )
                conn.commit()
            return row["id"]
        with self.db_lock:
            conn = self.sqlite_conn()
            try:
                conn.execute(
                    f"INSERT INTO committee_runs ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})",
                    tuple(row[k] for k in keys),
                )
                conn.commit()
            finally:
                conn.close()
        return row["id"]

    def list_runs(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        sql = "SELECT * FROM committee_runs WHERE user_id=%s ORDER BY created_at_utc DESC LIMIT %s"
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
            for key in ("votes_json", "evidence_json", "synthesis_json"):
                try:
                    row[key] = json.loads(row.get(key) or "[]" if key == "votes_json" else row.get(key) or "{}")
                except Exception:
                    row[key] = [] if key == "votes_json" else {}
        return rows
