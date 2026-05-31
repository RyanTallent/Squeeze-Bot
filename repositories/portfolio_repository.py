from __future__ import annotations

import json
import uuid
from datetime import datetime
from threading import Lock
from typing import Any, Callable


class PortfolioRepository:
    def __init__(self, using_postgres: Callable[[], bool], pg_conn: Callable[[], Any], sqlite_conn: Callable[[], Any], db_lock: Lock):
        self.using_postgres = using_postgres
        self.pg_conn = pg_conn
        self.sqlite_conn = sqlite_conn
        self.db_lock = db_lock

    def get_or_create_default_portfolio(self, user_id: str) -> dict[str, Any]:
        existing = self.list_portfolios(user_id)
        if existing:
            return existing[0]
        portfolio = {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "name": "Default Portfolio",
            "base_currency": "USD",
            "goals_json": "{}",
            "created_at_utc": datetime.utcnow().isoformat(),
            "updated_at_utc": datetime.utcnow().isoformat(),
        }
        keys = list(portfolio.keys())
        if self.using_postgres():
            with self.pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"INSERT INTO portfolios ({', '.join(keys)}) VALUES ({', '.join('%(' + k + ')s' for k in keys)})",
                        portfolio,
                    )
                conn.commit()
        else:
            with self.db_lock:
                conn = self.sqlite_conn()
                try:
                    conn.execute(
                        f"INSERT INTO portfolios ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})",
                        tuple(portfolio[k] for k in keys),
                    )
                    conn.commit()
                finally:
                    conn.close()
        return portfolio

    def list_portfolios(self, user_id: str) -> list[dict[str, Any]]:
        sql = "SELECT * FROM portfolios WHERE user_id=%s ORDER BY created_at_utc ASC"
        if self.using_postgres():
            with self.pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, (user_id,))
                    rows = [dict(r) for r in cur.fetchall()]
        else:
            conn = self.sqlite_conn()
            try:
                cur = conn.cursor()
                cur.execute(sql.replace("%s", "?"), (user_id,))
                rows = [dict(r) for r in cur.fetchall()]
            finally:
                conn.close()
        for r in rows:
            try:
                r["goals_json"] = json.loads(r.get("goals_json") or "{}")
            except Exception:
                r["goals_json"] = {}
        return rows

    def upsert_holding(self, user_id: str, portfolio_id: str, payload: dict[str, Any]) -> str:
        ticker = (payload.get("ticker") or "").upper().strip()
        if not ticker:
            raise ValueError("ticker is required")
        existing = self._find_holding(user_id, portfolio_id, ticker)
        holding_id = existing["id"] if existing else str(uuid.uuid4())
        now = datetime.utcnow().isoformat()
        row = {
            "id": holding_id,
            "user_id": user_id,
            "portfolio_id": portfolio_id,
            "ticker": ticker,
            "shares": float(payload.get("shares") or 0),
            "average_cost": float(payload.get("average_cost") or 0),
            "current_price": float(payload.get("current_price") or payload.get("average_cost") or 0),
            "sector": payload.get("sector") or "Unknown Sector",
            "industry": payload.get("industry") or "Unknown Industry",
            "theme": payload.get("theme") or "",
            "realized_pnl": payload.get("realized_pnl"),
            "notes": payload.get("notes") or "",
            "created_at_utc": existing.get("created_at_utc") if existing else now,
            "updated_at_utc": now,
        }
        if existing:
            self._update_holding(row)
        else:
            self._insert_holding(row)
        return holding_id

    def list_holdings(self, user_id: str, portfolio_id: str | None = None) -> list[dict[str, Any]]:
        where = ["user_id=%s"]
        params: list[Any] = [user_id]
        if portfolio_id:
            where.append("portfolio_id=%s")
            params.append(portfolio_id)
        sql = f"SELECT * FROM portfolio_holdings WHERE {' AND '.join(where)} ORDER BY ticker ASC"
        if self.using_postgres():
            with self.pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    return [dict(r) for r in cur.fetchall()]
        conn = self.sqlite_conn()
        try:
            cur = conn.cursor()
            cur.execute(sql.replace("%s", "?"), params)
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def save_snapshot(self, user_id: str, portfolio_id: str, analysis: dict[str, Any]) -> str:
        snapshot = {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "portfolio_id": portfolio_id,
            "total_value": analysis.get("portfolio_value") or 0,
            "risk_label": (analysis.get("risk") or {}).get("label"),
            "snapshot_json": json.dumps(analysis, default=str),
            "created_at_utc": datetime.utcnow().isoformat(),
        }
        keys = list(snapshot.keys())
        if self.using_postgres():
            with self.pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"INSERT INTO portfolio_snapshots ({', '.join(keys)}) VALUES ({', '.join('%(' + k + ')s' for k in keys)})",
                        snapshot,
                    )
                conn.commit()
            return snapshot["id"]
        with self.db_lock:
            conn = self.sqlite_conn()
            try:
                conn.execute(
                    f"INSERT INTO portfolio_snapshots ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})",
                    tuple(snapshot[k] for k in keys),
                )
                conn.commit()
            finally:
                conn.close()
        return snapshot["id"]

    def _find_holding(self, user_id: str, portfolio_id: str, ticker: str) -> dict[str, Any] | None:
        sql = "SELECT * FROM portfolio_holdings WHERE user_id=%s AND portfolio_id=%s AND ticker=%s LIMIT 1"
        params = (user_id, portfolio_id, ticker)
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

    def _insert_holding(self, row: dict[str, Any]):
        keys = list(row.keys())
        if self.using_postgres():
            with self.pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"INSERT INTO portfolio_holdings ({', '.join(keys)}) VALUES ({', '.join('%(' + k + ')s' for k in keys)})",
                        row,
                    )
                conn.commit()
            return
        with self.db_lock:
            conn = self.sqlite_conn()
            try:
                conn.execute(
                    f"INSERT INTO portfolio_holdings ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})",
                    tuple(row[k] for k in keys),
                )
                conn.commit()
            finally:
                conn.close()

    def _update_holding(self, row: dict[str, Any]):
        fields = ["shares", "average_cost", "current_price", "sector", "industry", "theme", "realized_pnl", "notes", "updated_at_utc"]
        if self.using_postgres():
            with self.pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"UPDATE portfolio_holdings SET {', '.join(f + '=%s' for f in fields)} WHERE id=%s AND user_id=%s",
                        [row[f] for f in fields] + [row["id"], row["user_id"]],
                    )
                conn.commit()
            return
        with self.db_lock:
            conn = self.sqlite_conn()
            try:
                conn.execute(
                    f"UPDATE portfolio_holdings SET {', '.join(f + '=?' for f in fields)} WHERE id=? AND user_id=?",
                    [row[f] for f in fields] + [row["id"], row["user_id"]],
                )
                conn.commit()
            finally:
                conn.close()
