from __future__ import annotations

import json
import uuid
from datetime import datetime
from threading import Lock
from typing import Any, Callable


class AlertRepository:
    def __init__(
        self,
        using_postgres: Callable[[], bool],
        pg_conn: Callable[[], Any],
        sqlite_conn: Callable[[], Any],
        db_lock: Lock,
    ):
        self.using_postgres = using_postgres
        self.pg_conn = pg_conn
        self.sqlite_conn = sqlite_conn
        self.db_lock = db_lock

    def create_alert(
        self,
        user_id: str,
        alert_type: str,
        ticker: str,
        message: str,
        urgency: str = "normal",
        importance: str = "watchlist",
        confidence: float | None = None,
        related_entity_type: str | None = None,
        related_entity_id: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> str:
        row = {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "alert_type": alert_type,
            "ticker": (ticker or "").upper(),
            "related_entity_type": related_entity_type,
            "related_entity_id": related_entity_id,
            "urgency": urgency,
            "importance": importance,
            "confidence": confidence,
            "message": message,
            "evidence": json.dumps(evidence or {}, default=str),
            "status": "OPEN",
            "delivered_at_utc": None,
            "created_at_utc": datetime.utcnow().isoformat(),
        }
        keys = list(row.keys())
        if self.using_postgres():
            with self.pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"INSERT INTO alerts ({', '.join(keys)}) VALUES ({', '.join('%(' + k + ')s' for k in keys)})",
                        row,
                    )
                conn.commit()
            return row["id"]

        with self.db_lock:
            conn = self.sqlite_conn()
            try:
                conn.execute(
                    f"INSERT INTO alerts ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})",
                    tuple(row[k] for k in keys),
                )
                conn.commit()
            finally:
                conn.close()
        return row["id"]

    def list_alerts(self, user_id: str, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        where = ["user_id = %s"]
        params: list[Any] = [user_id]
        if status:
            where.append("status = %s")
            params.append(status.upper())
        sql = f"SELECT * FROM alerts WHERE {' AND '.join(where)} ORDER BY created_at_utc DESC LIMIT {int(limit)}"
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
                row["evidence"] = json.loads(row.get("evidence") or "{}")
            except Exception:
                row["evidence"] = {}
        return rows

    def update_status(self, user_id: str, alert_id: str, status: str) -> bool:
        st = (status or "").upper()
        if st not in ("OPEN", "ACKNOWLEDGED", "RESOLVED", "DISMISSED"):
            raise ValueError("Invalid alert status")
        if self.using_postgres():
            with self.pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE alerts SET status=%s WHERE id=%s AND user_id=%s", (st, alert_id, user_id))
                    updated = cur.rowcount
                conn.commit()
            return bool(updated)

        with self.db_lock:
            conn = self.sqlite_conn()
            try:
                cur = conn.cursor()
                cur.execute("UPDATE alerts SET status=? WHERE id=? AND user_id=?", (st, alert_id, user_id))
                updated = cur.rowcount
                conn.commit()
            finally:
                conn.close()
        return bool(updated)
