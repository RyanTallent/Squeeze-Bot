from __future__ import annotations

import json
from datetime import datetime
from threading import Lock
from typing import Any, Callable


class TradePlanRepository:
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

    def save_plan(self, user_id: str, plan: dict[str, Any], scanner_row: dict[str, Any] | None = None) -> str:
        now = datetime.utcnow().isoformat()
        plan_id = plan["id"]
        intel = (scanner_row or {}).get("intelligence") or {}
        row = {
            "id": plan_id,
            "user_id": user_id,
            "ticker": (plan.get("ticker") or "").upper(),
            "source_scan_id": (scanner_row or {}).get("scan_id"),
            "setup_type": plan.get("setup_type"),
            "setup_grade": plan.get("setup_grade") or intel.get("setup_grade"),
            "plan_style": plan.get("plan_style") or "balanced",
            "entry_zone_low": plan.get("entry_zone_low"),
            "entry_zone_high": plan.get("entry_zone_high"),
            "trigger_price": plan.get("trigger_price"),
            "chase_threshold": plan.get("chase_threshold"),
            "stop_price": plan.get("stop_price"),
            "target_1": plan.get("target_1"),
            "target_2": plan.get("target_2"),
            "target_3": plan.get("target_3"),
            "risk_reward": plan.get("risk_reward"),
            "confidence": plan.get("confidence"),
            "conviction": plan.get("conviction"),
            "status": "ACTIVE",
            "decision_status": "watched",
            "outcome": None,
            "user_notes": "",
            "outcome_notes": "",
            "valid_conditions": json.dumps(plan.get("valid_conditions") or []),
            "invalidation_conditions": json.dumps(plan.get("invalidation_conditions") or []),
            "plan_json": json.dumps(plan, default=str),
            "created_at_utc": now,
            "updated_at_utc": now,
        }
        if self.using_postgres():
            with self.pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO trade_plans (
                          id, user_id, ticker, source_scan_id, setup_type, setup_grade, plan_style,
                          entry_zone_low, entry_zone_high, trigger_price, chase_threshold,
                          stop_price, target_1, target_2, target_3, risk_reward,
                          confidence, conviction, status, decision_status, outcome, user_notes, outcome_notes,
                          valid_conditions, invalidation_conditions, plan_json, created_at_utc, updated_at_utc
                        ) VALUES (
                          %(id)s, %(user_id)s, %(ticker)s, %(source_scan_id)s, %(setup_type)s, %(setup_grade)s, %(plan_style)s,
                          %(entry_zone_low)s, %(entry_zone_high)s, %(trigger_price)s, %(chase_threshold)s,
                          %(stop_price)s, %(target_1)s, %(target_2)s, %(target_3)s, %(risk_reward)s,
                          %(confidence)s, %(conviction)s, %(status)s, %(decision_status)s, %(outcome)s, %(user_notes)s, %(outcome_notes)s,
                          %(valid_conditions)s, %(invalidation_conditions)s, %(plan_json)s, %(created_at_utc)s, %(updated_at_utc)s
                        )
                        ON CONFLICT (id) DO UPDATE SET
                          updated_at_utc=EXCLUDED.updated_at_utc,
                          plan_json=EXCLUDED.plan_json
                        """,
                        row,
                    )
                conn.commit()
            return plan_id

        with self.db_lock:
            conn = self.sqlite_conn()
            try:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO trade_plans (
                      id, user_id, ticker, source_scan_id, setup_type, setup_grade, plan_style,
                      entry_zone_low, entry_zone_high, trigger_price, chase_threshold,
                      stop_price, target_1, target_2, target_3, risk_reward,
                      confidence, conviction, status, decision_status, outcome, user_notes, outcome_notes,
                      valid_conditions, invalidation_conditions, plan_json, created_at_utc, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    tuple(row[k] for k in (
                        "id", "user_id", "ticker", "source_scan_id", "setup_type", "setup_grade", "plan_style",
                        "entry_zone_low", "entry_zone_high", "trigger_price", "chase_threshold",
                        "stop_price", "target_1", "target_2", "target_3", "risk_reward",
                        "confidence", "conviction", "status", "decision_status", "outcome", "user_notes", "outcome_notes",
                        "valid_conditions", "invalidation_conditions", "plan_json", "created_at_utc", "updated_at_utc"
                    )),
                )
                conn.commit()
            finally:
                conn.close()
        return plan_id

    def list_plans(self, user_id: str, status: str | None = None, limit: int = 250) -> list[dict[str, Any]]:
        where = ["user_id = %s"]
        params: list[Any] = [user_id]
        if status:
            where.append("status = %s")
            params.append(status.upper())
        sql = f"SELECT * FROM trade_plans WHERE {' AND '.join(where)} ORDER BY created_at_utc DESC LIMIT {int(limit)}"

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

        for r in rows:
            for key in ("valid_conditions", "invalidation_conditions"):
                try:
                    r[key] = json.loads(r.get(key) or "[]")
                except Exception:
                    r[key] = []
            try:
                r["plan_json"] = json.loads(r.get("plan_json") or "{}")
            except Exception:
                r["plan_json"] = {}
        return rows

    def update_decision(self, user_id: str, plan_id: str, decision_status: str, notes: str = "") -> bool:
        decision = (decision_status or "").lower()
        if decision not in ("watched", "traded", "skipped"):
            raise ValueError("decision_status must be watched, traded, or skipped")
        status = "SKIPPED" if decision == "skipped" else ("TRIGGERED" if decision == "traded" else "ACTIVE")
        return self._update_fields(user_id, plan_id, {"decision_status": decision, "user_notes": notes, "status": status})

    def update_outcome(self, user_id: str, plan_id: str, outcome: str, notes: str = "") -> bool:
        out = (outcome or "").lower()
        if out not in ("winner", "loser", "break_even"):
            raise ValueError("outcome must be winner, loser, or break_even")
        return self._update_fields(user_id, plan_id, {"outcome": out, "outcome_notes": notes, "status": "COMPLETED"})

    def _update_fields(self, user_id: str, plan_id: str, fields: dict[str, Any]) -> bool:
        fields["updated_at_utc"] = datetime.utcnow().isoformat()
        assignments_pg = ", ".join(f"{k}=%s" for k in fields.keys())
        assignments_sqlite = ", ".join(f"{k}=?" for k in fields.keys())
        values = list(fields.values())
        if self.using_postgres():
            with self.pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"UPDATE trade_plans SET {assignments_pg} WHERE id=%s AND user_id=%s",
                        values + [plan_id, user_id],
                    )
                    updated = cur.rowcount
                conn.commit()
            return bool(updated)

        with self.db_lock:
            conn = self.sqlite_conn()
            try:
                cur = conn.cursor()
                cur.execute(
                    f"UPDATE trade_plans SET {assignments_sqlite} WHERE id=? AND user_id=?",
                    values + [plan_id, user_id],
                )
                updated = cur.rowcount
                conn.commit()
            finally:
                conn.close()
        return bool(updated)
