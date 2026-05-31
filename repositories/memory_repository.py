from __future__ import annotations

import uuid
from datetime import datetime
from threading import Lock
from typing import Any, Callable


class MemoryRepository:
    def __init__(self, using_postgres: Callable[[], bool], pg_conn: Callable[[], Any], sqlite_conn: Callable[[], Any], db_lock: Lock):
        self.using_postgres = using_postgres
        self.pg_conn = pg_conn
        self.sqlite_conn = sqlite_conn
        self.db_lock = db_lock

    def upsert_memory(self, user_id: str, item: dict[str, Any]) -> str:
        existing = self._find_existing(user_id, item)
        now = datetime.utcnow().isoformat()
        if existing:
            memory_id = existing["id"]
            confidence = max(float(existing.get("confidence") or 0), float(item.get("confidence") or 0))
            evidence_count = max(int(existing.get("evidence_count") or 0), int(item.get("evidence_count") or 0))
            self._update(memory_id, user_id, confidence, evidence_count, now)
            return memory_id

        memory_id = str(uuid.uuid4())
        row = {
            "id": memory_id,
            "user_id": user_id,
            "memory_type": item["memory_type"],
            "belief_type": item["belief_type"],
            "topic": item.get("topic"),
            "statement": item["statement"],
            "confidence": item.get("confidence") or 0,
            "evidence_count": item.get("evidence_count") or 0,
            "source_module": item.get("source_module"),
            "supporting_record_ids": "",
            "contradicting_record_ids": "",
            "created_at_utc": now,
            "updated_at_utc": now,
        }
        keys = list(row.keys())
        if self.using_postgres():
            with self.pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"INSERT INTO praetor_memory_items ({', '.join(keys)}) VALUES ({', '.join('%(' + k + ')s' for k in keys)})",
                        row,
                    )
                conn.commit()
            return memory_id

        with self.db_lock:
            conn = self.sqlite_conn()
            try:
                conn.execute(
                    f"INSERT INTO praetor_memory_items ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})",
                    tuple(row[k] for k in keys),
                )
                conn.commit()
            finally:
                conn.close()
        return memory_id

    def list_memory(self, user_id: str, limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT * FROM praetor_memory_items WHERE user_id=%s ORDER BY updated_at_utc DESC LIMIT %s"
        if self.using_postgres():
            with self.pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, (user_id, limit))
                    return [dict(r) for r in cur.fetchall()]

        conn = self.sqlite_conn()
        try:
            cur = conn.cursor()
            cur.execute(sql.replace("%s", "?"), (user_id, limit))
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def _find_existing(self, user_id: str, item: dict[str, Any]) -> dict[str, Any] | None:
        sql = """
        SELECT * FROM praetor_memory_items
        WHERE user_id=%s AND memory_type=%s AND belief_type=%s AND topic=%s AND statement=%s
        LIMIT 1
        """
        params = (user_id, item["memory_type"], item["belief_type"], item.get("topic"), item["statement"])
        if self.using_postgres():
            with self.pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    row = cur.fetchone()
                    return dict(row) if row else None
        conn = self.sqlite_conn()
        try:
            cur = conn.cursor()
            cur.execute(sql.replace("%s", "?"), params)
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def _update(self, memory_id: str, user_id: str, confidence: float, evidence_count: int, updated_at: str):
        if self.using_postgres():
            with self.pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE praetor_memory_items SET confidence=%s, evidence_count=%s, updated_at_utc=%s WHERE id=%s AND user_id=%s",
                        (confidence, evidence_count, updated_at, memory_id, user_id),
                    )
                conn.commit()
            return
        with self.db_lock:
            conn = self.sqlite_conn()
            try:
                conn.execute(
                    "UPDATE praetor_memory_items SET confidence=?, evidence_count=?, updated_at_utc=? WHERE id=? AND user_id=?",
                    (confidence, evidence_count, updated_at, memory_id, user_id),
                )
                conn.commit()
            finally:
                conn.close()
