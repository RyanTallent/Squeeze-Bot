from __future__ import annotations

import alerts
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import statistics
import threading
import time
import traceback
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Generator, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

import scanner  # your scanner.py
from praetor_context import build_scanner_context
from praetor_service import PraetorService, response_to_dict
from praetor_providers import ai_provider_status
from playbook_engine import calculate_playbook_stats
from memory_engine import build_memory_updates
from discovery_engine import build_discovery_candidates, build_journal_discovery_candidates
from discovery_engine_v2 import build_discovery_v2_candidates, summarize_discoveries
from journal_engine import build_journal_memory_updates, build_journal_report
from risk_engine import build_risk_report
from monitoring_engine import monitor_trade_plans
from alert_engine import build_smart_alert, smart_alert_to_repo_kwargs
from briefing_engine import build_briefing
from committee_engine import run_investment_committee
from command_center_engine import build_command_center
from monitor_scheduler import build_monitoring_health
from portfolio_engine import analyze_portfolio
from wealth_engine import build_wealth_plan
from research_engine import build_institutional_research
from fundamental_engine import build_fundamental_analysis
from ai_synthesis_engine import synthesize as synthesize_ai
from data_providers.fmp_provider import FMPProvider, fmp_status
from data_providers.sec_provider import sec_status
from sector_frameworks import available_sector_frameworks
from ticker_normalization import ticker_normalization_metadata
from services.praetor_orchestrator import PraetorDataLoaders, PraetorOrchestrator, PraetorRepositories
from repositories.alert_repository import AlertRepository
from repositories.briefing_repository import BriefingRepository
from repositories.committee_repository import CommitteeRepository
from repositories.discovery_repository import DiscoveryRepository
from repositories.memory_repository import MemoryRepository
from repositories.playbook_repository import PlaybookRepository
from repositories.portfolio_repository import PortfolioRepository
from repositories.research_repository import ResearchRepository
from repositories.trade_plan_repository import TradePlanRepository

# Optional Postgres (only used if DATABASE_URL is set)
try:
    import psycopg
    from psycopg.rows import dict_row
except Exception:
    psycopg = None
    dict_row = None

# -------------------- timezone helpers (CT) --------------------
try:
    from zoneinfo import ZoneInfo

    CT_TZ = ZoneInfo("America/Chicago")
except Exception:
    CT_TZ = None


def now_ct() -> datetime:
    return datetime.now(tz=CT_TZ) if CT_TZ else datetime.now()


def now_ct_str() -> str:
    dt = now_ct()
    try:
        return dt.strftime("%-I:%M %p CT")
    except Exception:
        return dt.strftime("%I:%M %p CT").lstrip("0")


def ct_date(dt: datetime | None = None) -> str:
    dt = dt or now_ct()
    return dt.strftime("%Y-%m-%d")


def yesterday_ct_date() -> str:
    return (now_ct() - timedelta(days=1)).strftime("%Y-%m-%d")


# -------------------- Storage (Postgres if available, else SQLite fallback) --------------------
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
OUT_DIR = BASE_DIR / "outputs"
OUT_DIR.mkdir(exist_ok=True)

SQLITE_PATH = OUT_DIR / "trades.sqlite3"
DB_LOCK = threading.Lock()


def using_postgres() -> bool:
    return bool(DATABASE_URL) and psycopg is not None


def pg_conn():
    url = DATABASE_URL
    if "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=8)


def sqlite_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(SQLITE_PATH), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
    except Exception:
        pass
    return conn


def _table_has_column_sqlite(conn: sqlite3.Connection, table: str, col: str) -> bool:
    try:
        cur = conn.cursor()
        cur.execute(f"PRAGMA table_info({table})")
        cols = [r[1] for r in cur.fetchall()]
        return col in cols
    except Exception:
        return False


def _ensure_schema_migrations():
    """
    Minimal, safe migrations for existing deployed databases.

    CREATE TABLE IF NOT EXISTS does not add columns to an existing table, so
    older Render/Postgres databases can be missing scanner/journal fields that
    newer query paths expect.
    """
    nullable_trade_cols = {
        "scan_id": "TEXT",
        "scan_date_ct": "TEXT",
        "bucket": "TEXT",
        "subtype": "TEXT",
        "confidence": "REAL",
        "plan": "TEXT",
        "trigger": "REAL",
        "stop": "REAL",
        "scan_close": "REAL",
        "move_pct": "REAL",
        "dollar_vol": "REAL",
        "range_pct": "REAL",
        "hold_pct": "REAL",
        "rel_vol": "REAL",
        "si_pct_ff": "REAL",
        "ctb": "REAL",
        "avail": "REAL",
        "entry_price": "REAL",
        "entry_time_ct": "TEXT",
        "exit_price": "REAL",
        "exit_time_ct": "TEXT",
        "shares": "REAL",
        "review_flags": "TEXT",
        "review_text": "TEXT",
        "reviewed_at_utc": "TIMESTAMPTZ" if using_postgres() else "TEXT",
    }

    if using_postgres():
        try:
            with pg_conn() as conn:
                with conn.cursor() as cur:
                    for col, typ in nullable_trade_cols.items():
                        cur.execute(f"ALTER TABLE trades ADD COLUMN IF NOT EXISTS {col} {typ};")
                conn.commit()
        except Exception:
            pass
    else:
        with DB_LOCK:
            conn = sqlite_conn()
            try:
                for col, typ in nullable_trade_cols.items():
                    if not _table_has_column_sqlite(conn, "trades", col):
                        conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {typ};")
                conn.commit()
            except Exception:
                pass
            finally:
                conn.close()


def _ensure_praetor_schema_migrations():
    trade_plan_cols = {
        "setup_grade": "TEXT",
        "decision_status": "TEXT",
        "outcome": "TEXT",
        "user_notes": "TEXT",
        "outcome_notes": "TEXT",
    }

    if using_postgres():
        try:
            with pg_conn() as conn:
                with conn.cursor() as cur:
                    for col, typ in trade_plan_cols.items():
                        cur.execute(f"ALTER TABLE trade_plans ADD COLUMN IF NOT EXISTS {col} {typ};")
                    discovery_cols = {
                        "category": "TEXT",
                        "impact_score": "REAL",
                        "importance": "TEXT",
                        "urgency": "TEXT",
                        "priority": "TEXT",
                    }
                    for col, typ in discovery_cols.items():
                        cur.execute(f"ALTER TABLE discoveries ADD COLUMN IF NOT EXISTS {col} {typ};")
                    alert_cols = {
                        "category": "TEXT",
                        "priority": "TEXT",
                        "source_modules": "TEXT",
                        "explanation": "TEXT",
                    }
                    for col, typ in alert_cols.items():
                        cur.execute(f"ALTER TABLE alerts ADD COLUMN IF NOT EXISTS {col} {typ};")
                conn.commit()
        except Exception:
            pass
        return

    with DB_LOCK:
        conn = sqlite_conn()
        try:
            for col, typ in trade_plan_cols.items():
                if not _table_has_column_sqlite(conn, "trade_plans", col):
                    conn.execute(f"ALTER TABLE trade_plans ADD COLUMN {col} {typ};")
            discovery_cols = {
                "category": "TEXT",
                "impact_score": "REAL",
                "importance": "TEXT",
                "urgency": "TEXT",
                "priority": "TEXT",
            }
            for col, typ in discovery_cols.items():
                if not _table_has_column_sqlite(conn, "discoveries", col):
                    conn.execute(f"ALTER TABLE discoveries ADD COLUMN {col} {typ};")
            alert_cols = {
                "category": "TEXT",
                "priority": "TEXT",
                "source_modules": "TEXT",
                "explanation": "TEXT",
            }
            for col, typ in alert_cols.items():
                if not _table_has_column_sqlite(conn, "alerts", col):
                    conn.execute(f"ALTER TABLE alerts ADD COLUMN {col} {typ};")
            conn.commit()
        except Exception:
            pass
        finally:
            conn.close()


def db_init():
    schema_sql = """
    CREATE TABLE IF NOT EXISTS trades (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      created_at_utc TEXT NOT NULL,

      scan_id TEXT,
      scan_date_ct TEXT,

      ticker TEXT NOT NULL,
      bucket TEXT,
      subtype TEXT,
      confidence REAL,
      plan TEXT,

      trigger REAL,
      stop REAL,
      scan_close REAL,
      move_pct REAL,
      dollar_vol REAL,
      range_pct REAL,
      hold_pct REAL,
      rel_vol REAL,
      si_pct_ff REAL,
      ctb REAL,
      avail REAL,

      entry_price REAL,
      entry_time_ct TEXT,
      exit_price REAL,
      exit_time_ct TEXT,
      shares REAL,

      review_flags TEXT
    );
    """

    if using_postgres():
        with pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(schema_sql.replace("created_at_utc TEXT", "created_at_utc TIMESTAMPTZ"))
            conn.commit()
    else:
        with DB_LOCK:
            conn = sqlite_conn()
            try:
                conn.execute(schema_sql)
                conn.commit()
            finally:
                conn.close()

    _ensure_schema_migrations()

    signals_sql = """
    CREATE TABLE IF NOT EXISTS signals (
      id TEXT PRIMARY KEY,
      created_at_utc TEXT NOT NULL,

      scan_id TEXT,
      scan_date_ct TEXT NOT NULL,

      ticker TEXT NOT NULL,
      confidence REAL,
      entry REAL NOT NULL,
      win_px REAL NOT NULL,
      loss_px REAL NOT NULL,

      status TEXT NOT NULL,
      triggered_at_utc TEXT,
      resolved_at_utc TEXT,

      max_after_trigger REAL,
      min_after_trigger REAL,

      UNIQUE(scan_date_ct, ticker)
    );
    """

    kv_sql = """
    CREATE TABLE IF NOT EXISTS kv (
      k TEXT PRIMARY KEY,
      v TEXT
    );
    """

    users_sql = """
    CREATE TABLE IF NOT EXISTS users (
      id TEXT PRIMARY KEY,
      email TEXT NOT NULL UNIQUE,
      password_hash TEXT NOT NULL,
      plan_code TEXT NOT NULL DEFAULT 'free_trial',
      subscription_status TEXT NOT NULL DEFAULT 'trial',
      lifetime_scans_used INTEGER NOT NULL DEFAULT 0,
      created_at_utc TEXT NOT NULL
    );
    """

    sessions_sql = """
    CREATE TABLE IF NOT EXISTS sessions (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      token_hash TEXT NOT NULL UNIQUE,
      created_at_utc TEXT NOT NULL,
      expires_at_utc TEXT NOT NULL
    );
    """

    scan_usage_sql = """
    CREATE TABLE IF NOT EXISTS scan_usage (
      user_id TEXT NOT NULL,
      week_start_ct TEXT NOT NULL,
      scans_used INTEGER NOT NULL DEFAULT 0,
      ortex_scans_used INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY (user_id, week_start_ct)
    );
    """

    praetor_interactions_sql = """
    CREATE TABLE IF NOT EXISTS praetor_interactions (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      page_context TEXT,
      module TEXT,
      topic TEXT,
      user_message TEXT,
      praetor_response TEXT,
      context_json TEXT,
      tools_used TEXT,
      created_at_utc TEXT NOT NULL
    );
    """

    praetor_memory_sql = """
    CREATE TABLE IF NOT EXISTS praetor_memory_items (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      memory_type TEXT NOT NULL,
      belief_type TEXT NOT NULL,
      topic TEXT,
      statement TEXT NOT NULL,
      confidence REAL NOT NULL DEFAULT 0,
      evidence_count INTEGER NOT NULL DEFAULT 0,
      source_module TEXT,
      supporting_record_ids TEXT,
      contradicting_record_ids TEXT,
      created_at_utc TEXT NOT NULL,
      updated_at_utc TEXT NOT NULL
    );
    """

    playbook_rules_sql = """
    CREATE TABLE IF NOT EXISTS playbook_rules (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      setup_type TEXT,
      rule_text TEXT NOT NULL,
      rule_category TEXT,
      confidence REAL NOT NULL DEFAULT 0,
      evidence_count INTEGER NOT NULL DEFAULT 0,
      active INTEGER NOT NULL DEFAULT 1,
      created_at_utc TEXT NOT NULL,
      updated_at_utc TEXT NOT NULL
    );
    """

    trade_plans_sql = """
    CREATE TABLE IF NOT EXISTS trade_plans (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      ticker TEXT NOT NULL,
      source_scan_id TEXT,
      setup_type TEXT,
      plan_style TEXT NOT NULL,
      entry_zone_low REAL,
      entry_zone_high REAL,
      trigger_price REAL,
      chase_threshold REAL,
      stop_price REAL,
      target_1 REAL,
      target_2 REAL,
      target_3 REAL,
      risk_reward REAL,
      confidence REAL,
      conviction REAL,
      status TEXT NOT NULL DEFAULT 'ACTIVE',
      setup_grade TEXT,
      decision_status TEXT,
      outcome TEXT,
      user_notes TEXT,
      outcome_notes TEXT,
      valid_conditions TEXT,
      invalidation_conditions TEXT,
      plan_json TEXT,
      created_at_utc TEXT NOT NULL,
      updated_at_utc TEXT NOT NULL
    );
    """

    alerts_sql = """
    CREATE TABLE IF NOT EXISTS alerts (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      alert_type TEXT NOT NULL,
      ticker TEXT,
      related_entity_type TEXT,
      related_entity_id TEXT,
      category TEXT,
      priority TEXT,
      urgency TEXT,
      importance TEXT,
      confidence REAL,
      message TEXT NOT NULL,
      explanation TEXT,
      source_modules TEXT,
      evidence TEXT,
      status TEXT NOT NULL DEFAULT 'OPEN',
      delivered_at_utc TEXT,
      created_at_utc TEXT NOT NULL
    );
    """

    playbook_snapshots_sql = """
    CREATE TABLE IF NOT EXISTS playbook_snapshots (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      sample_size INTEGER NOT NULL DEFAULT 0,
      win_rate REAL,
      expectancy REAL,
      snapshot_json TEXT NOT NULL,
      created_at_utc TEXT NOT NULL
    );
    """

    discoveries_sql = """
    CREATE TABLE IF NOT EXISTS discoveries (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      discovery_type TEXT NOT NULL,
      category TEXT,
      title TEXT NOT NULL,
      description TEXT,
      confidence REAL NOT NULL DEFAULT 0,
      evidence_count INTEGER NOT NULL DEFAULT 0,
      impact_score REAL,
      importance TEXT,
      urgency TEXT,
      priority TEXT,
      source_module TEXT,
      evidence_json TEXT,
      status TEXT NOT NULL DEFAULT 'OPEN',
      created_at_utc TEXT NOT NULL,
      updated_at_utc TEXT NOT NULL
    );
    """

    briefing_runs_sql = """
    CREATE TABLE IF NOT EXISTS briefing_runs (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      briefing_type TEXT NOT NULL,
      priority TEXT,
      title TEXT,
      summary TEXT,
      content_json TEXT NOT NULL,
      source_context_json TEXT,
      created_at_utc TEXT NOT NULL
    );
    """

    committee_runs_sql = """
    CREATE TABLE IF NOT EXISTS committee_runs (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      committee_type TEXT NOT NULL,
      consensus TEXT,
      final_recommendation TEXT,
      confidence REAL,
      votes_json TEXT NOT NULL,
      evidence_json TEXT,
      synthesis_json TEXT,
      created_at_utc TEXT NOT NULL
    );
    """

    monitoring_runs_sql = """
    CREATE TABLE IF NOT EXISTS monitoring_runs (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      active_plan_count INTEGER NOT NULL DEFAULT 0,
      stale_plan_count INTEGER NOT NULL DEFAULT 0,
      open_alert_count INTEGER NOT NULL DEFAULT 0,
      failed_check_count INTEGER NOT NULL DEFAULT 0,
      health_json TEXT NOT NULL,
      created_at_utc TEXT NOT NULL
    );
    """

    notification_preferences_sql = """
    CREATE TABLE IF NOT EXISTS notification_preferences (
      user_id TEXT PRIMARY KEY,
      alert_level TEXT NOT NULL DEFAULT 'high_and_critical',
      cooldown_minutes INTEGER NOT NULL DEFAULT 20,
      email_enabled INTEGER NOT NULL DEFAULT 0,
      sms_enabled INTEGER NOT NULL DEFAULT 0,
      push_enabled INTEGER NOT NULL DEFAULT 0,
      quiet_hours_json TEXT,
      updated_at_utc TEXT NOT NULL
    );
    """

    portfolios_sql = """
    CREATE TABLE IF NOT EXISTS portfolios (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      name TEXT NOT NULL,
      base_currency TEXT NOT NULL DEFAULT 'USD',
      goals_json TEXT,
      created_at_utc TEXT NOT NULL,
      updated_at_utc TEXT NOT NULL
    );
    """

    portfolio_holdings_sql = """
    CREATE TABLE IF NOT EXISTS portfolio_holdings (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      portfolio_id TEXT NOT NULL,
      ticker TEXT NOT NULL,
      shares REAL NOT NULL DEFAULT 0,
      average_cost REAL NOT NULL DEFAULT 0,
      current_price REAL NOT NULL DEFAULT 0,
      sector TEXT,
      industry TEXT,
      theme TEXT,
      realized_pnl REAL,
      notes TEXT,
      created_at_utc TEXT NOT NULL,
      updated_at_utc TEXT NOT NULL,
      UNIQUE(user_id, portfolio_id, ticker)
    );
    """

    portfolio_snapshots_sql = """
    CREATE TABLE IF NOT EXISTS portfolio_snapshots (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      portfolio_id TEXT NOT NULL,
      total_value REAL NOT NULL DEFAULT 0,
      risk_label TEXT,
      snapshot_json TEXT NOT NULL,
      created_at_utc TEXT NOT NULL
    );
    """

    research_reports_sql = """
    CREATE TABLE IF NOT EXISTS research_reports (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      ticker TEXT NOT NULL,
      verdict TEXT,
      aggregate_score REAL,
      report_json TEXT NOT NULL,
      profile_json TEXT,
      ai_json TEXT,
      created_at_utc TEXT NOT NULL
    );
    """

    provider_cache_sql = """
    CREATE TABLE IF NOT EXISTS provider_cache (
      cache_key TEXT PRIMARY KEY,
      provider TEXT NOT NULL,
      response_json TEXT NOT NULL,
      fetched_at_utc TEXT NOT NULL,
      expires_at_utc TEXT NOT NULL
    );
    """

    if using_postgres():
        with pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(signals_sql.replace("created_at_utc TEXT", "created_at_utc TIMESTAMPTZ"))
                cur.execute(kv_sql)
                cur.execute(users_sql.replace("created_at_utc TEXT", "created_at_utc TIMESTAMPTZ"))
                cur.execute(sessions_sql.replace("created_at_utc TEXT", "created_at_utc TIMESTAMPTZ").replace("expires_at_utc TEXT", "expires_at_utc TIMESTAMPTZ"))
                cur.execute(scan_usage_sql)
                cur.execute(praetor_interactions_sql.replace("created_at_utc TEXT", "created_at_utc TIMESTAMPTZ"))
                cur.execute(
                    praetor_memory_sql.replace("created_at_utc TEXT", "created_at_utc TIMESTAMPTZ").replace("updated_at_utc TEXT", "updated_at_utc TIMESTAMPTZ")
                )
                cur.execute(
                    playbook_rules_sql.replace("created_at_utc TEXT", "created_at_utc TIMESTAMPTZ").replace("updated_at_utc TEXT", "updated_at_utc TIMESTAMPTZ")
                )
                cur.execute(
                    trade_plans_sql.replace("created_at_utc TEXT", "created_at_utc TIMESTAMPTZ").replace("updated_at_utc TEXT", "updated_at_utc TIMESTAMPTZ")
                )
                cur.execute(alerts_sql.replace("created_at_utc TEXT", "created_at_utc TIMESTAMPTZ").replace("delivered_at_utc TEXT", "delivered_at_utc TIMESTAMPTZ"))
                cur.execute(playbook_snapshots_sql.replace("created_at_utc TEXT", "created_at_utc TIMESTAMPTZ"))
                cur.execute(
                    discoveries_sql.replace("created_at_utc TEXT", "created_at_utc TIMESTAMPTZ").replace("updated_at_utc TEXT", "updated_at_utc TIMESTAMPTZ")
                )
                cur.execute(briefing_runs_sql.replace("created_at_utc TEXT", "created_at_utc TIMESTAMPTZ"))
                cur.execute(committee_runs_sql.replace("created_at_utc TEXT", "created_at_utc TIMESTAMPTZ"))
                cur.execute(monitoring_runs_sql.replace("created_at_utc TEXT", "created_at_utc TIMESTAMPTZ"))
                cur.execute(notification_preferences_sql.replace("updated_at_utc TEXT", "updated_at_utc TIMESTAMPTZ"))
                cur.execute(portfolios_sql.replace("created_at_utc TEXT", "created_at_utc TIMESTAMPTZ").replace("updated_at_utc TEXT", "updated_at_utc TIMESTAMPTZ"))
                cur.execute(portfolio_holdings_sql.replace("created_at_utc TEXT", "created_at_utc TIMESTAMPTZ").replace("updated_at_utc TEXT", "updated_at_utc TIMESTAMPTZ"))
                cur.execute(portfolio_snapshots_sql.replace("created_at_utc TEXT", "created_at_utc TIMESTAMPTZ"))
                cur.execute(research_reports_sql.replace("created_at_utc TEXT", "created_at_utc TIMESTAMPTZ"))
                cur.execute(
                    provider_cache_sql.replace("fetched_at_utc TEXT", "fetched_at_utc TIMESTAMPTZ").replace("expires_at_utc TEXT", "expires_at_utc TIMESTAMPTZ")
                )
            conn.commit()
    else:
        with DB_LOCK:
            conn = sqlite_conn()
            try:
                conn.execute(signals_sql)
                conn.execute(kv_sql)
                conn.execute(users_sql)
                conn.execute(sessions_sql)
                conn.execute(scan_usage_sql)
                conn.execute(praetor_interactions_sql)
                conn.execute(praetor_memory_sql)
                conn.execute(playbook_rules_sql)
                conn.execute(trade_plans_sql)
                conn.execute(alerts_sql)
                conn.execute(playbook_snapshots_sql)
                conn.execute(discoveries_sql)
                conn.execute(briefing_runs_sql)
                conn.execute(committee_runs_sql)
                conn.execute(monitoring_runs_sql)
                conn.execute(notification_preferences_sql)
                conn.execute(portfolios_sql)
                conn.execute(portfolio_holdings_sql)
                conn.execute(portfolio_snapshots_sql)
                conn.execute(research_reports_sql)
                conn.execute(provider_cache_sql)
                conn.commit()
            finally:
                conn.close()

    _ensure_praetor_schema_migrations()


# -------------------- KV helpers --------------------
def kv_get(key: str) -> str | None:
    if using_postgres():
        with pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT v FROM kv WHERE k=%s", (key,))
                row = cur.fetchone()
                return row["v"] if row else None
    conn = sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT v FROM kv WHERE k=?", (key,))
        row = cur.fetchone()
        return row["v"] if row else None
    finally:
        conn.close()


def kv_set(key: str, val: str):
    if using_postgres():
        with pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO kv (k,v) VALUES (%s,%s) "
                    "ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v",
                    (key, val),
                )
            conn.commit()
        return

    with DB_LOCK:
        conn = sqlite_conn()
        try:
            conn.execute("INSERT OR REPLACE INTO kv (k,v) VALUES (?,?)", (key, val))
            conn.commit()
        finally:
            conn.close()


def praetor_log_interaction(
    user_id: str,
    page_context: str,
    module: str,
    topic: str,
    user_message: str,
    praetor_response: str,
    context: dict[str, Any] | None = None,
    tools_used: list[str] | None = None,
) -> str:
    row = {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "page_context": page_context,
        "module": module,
        "topic": topic,
        "user_message": user_message,
        "praetor_response": praetor_response,
        "context_json": json.dumps(context or {}, default=str),
        "tools_used": json.dumps(tools_used or []),
        "created_at_utc": datetime.utcnow().isoformat(),
    }

    if using_postgres():
        with pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO praetor_interactions (
                      id, user_id, page_context, module, topic, user_message,
                      praetor_response, context_json, tools_used, created_at_utc
                    ) VALUES (
                      %(id)s, %(user_id)s, %(page_context)s, %(module)s, %(topic)s, %(user_message)s,
                      %(praetor_response)s, %(context_json)s, %(tools_used)s, %(created_at_utc)s
                    )
                    """,
                    row,
                )
            conn.commit()
        return row["id"]

    with DB_LOCK:
        conn = sqlite_conn()
        try:
            conn.execute(
                """
                INSERT INTO praetor_interactions (
                  id, user_id, page_context, module, topic, user_message,
                  praetor_response, context_json, tools_used, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row["user_id"],
                    row["page_context"],
                    row["module"],
                    row["topic"],
                    row["user_message"],
                    row["praetor_response"],
                    row["context_json"],
                    row["tools_used"],
                    row["created_at_utc"],
                ),
            )
            conn.commit()
        finally:
            conn.close()
    return row["id"]


def trade_plan_repo() -> TradePlanRepository:
    return TradePlanRepository(using_postgres, pg_conn, sqlite_conn, DB_LOCK)


def alert_repo() -> AlertRepository:
    return AlertRepository(using_postgres, pg_conn, sqlite_conn, DB_LOCK)


def memory_repo() -> MemoryRepository:
    return MemoryRepository(using_postgres, pg_conn, sqlite_conn, DB_LOCK)


def playbook_repo() -> PlaybookRepository:
    return PlaybookRepository(using_postgres, pg_conn, sqlite_conn, DB_LOCK)


def discovery_repo() -> DiscoveryRepository:
    return DiscoveryRepository(using_postgres, pg_conn, sqlite_conn, DB_LOCK)


def briefing_repo() -> BriefingRepository:
    return BriefingRepository(using_postgres, pg_conn, sqlite_conn, DB_LOCK)


def committee_repo() -> CommitteeRepository:
    return CommitteeRepository(using_postgres, pg_conn, sqlite_conn, DB_LOCK)


def portfolio_repo() -> PortfolioRepository:
    return PortfolioRepository(using_postgres, pg_conn, sqlite_conn, DB_LOCK)


def research_repo() -> ResearchRepository:
    return ResearchRepository(using_postgres, pg_conn, sqlite_conn, DB_LOCK)


def provider_cache_get(cache_key: str) -> dict[str, Any] | None:
    now = datetime.utcnow().isoformat()
    if using_postgres():
        with pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT response_json, fetched_at_utc, expires_at_utc FROM provider_cache WHERE cache_key=%s AND expires_at_utc>%s",
                    (cache_key, now),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return {"data": json.loads(row["response_json"]), "fetched_at": str(row["fetched_at_utc"]), "expires_at": str(row["expires_at_utc"])}

    conn = sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT response_json, fetched_at_utc, expires_at_utc FROM provider_cache WHERE cache_key=? AND expires_at_utc>?",
            (cache_key, now),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {"data": json.loads(row["response_json"]), "fetched_at": row["fetched_at_utc"], "expires_at": row["expires_at_utc"]}
    finally:
        conn.close()


def provider_cache_set(cache_key: str, provider: str, data: dict[str, Any] | list[Any], expires_at: datetime):
    row = {
        "cache_key": cache_key,
        "provider": provider,
        "response_json": json.dumps(data, default=str),
        "fetched_at_utc": datetime.utcnow().isoformat(),
        "expires_at_utc": expires_at.isoformat(),
    }
    if using_postgres():
        with pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO provider_cache (cache_key, provider, response_json, fetched_at_utc, expires_at_utc)
                    VALUES (%(cache_key)s, %(provider)s, %(response_json)s, %(fetched_at_utc)s, %(expires_at_utc)s)
                    ON CONFLICT (cache_key) DO UPDATE SET
                      provider=EXCLUDED.provider,
                      response_json=EXCLUDED.response_json,
                      fetched_at_utc=EXCLUDED.fetched_at_utc,
                      expires_at_utc=EXCLUDED.expires_at_utc
                    """,
                    row,
                )
            conn.commit()
        return

    with DB_LOCK:
        conn = sqlite_conn()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO provider_cache (cache_key, provider, response_json, fetched_at_utc, expires_at_utc)
                VALUES (?, ?, ?, ?, ?)
                """,
                (row["cache_key"], row["provider"], row["response_json"], row["fetched_at_utc"], row["expires_at_utc"]),
            )
            conn.commit()
        finally:
            conn.close()


def fmp_provider() -> FMPProvider:
    return FMPProvider(cache_get=provider_cache_get, cache_set=provider_cache_set)


def get_fundamental_analysis(ticker: str, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    bundle = fmp_provider().fundamentals_bundle(ticker)
    return build_fundamental_analysis(bundle, profile=profile)


def praetor_orchestrator() -> PraetorOrchestrator:
    return PraetorOrchestrator(
        PraetorRepositories(
            trade_plan_repo=trade_plan_repo(),
            alert_repo=alert_repo(),
            memory_repo=memory_repo(),
            discovery_repo=discovery_repo(),
            briefing_repo=briefing_repo(),
            committee_repo=committee_repo(),
            research_repo=research_repo(),
        ),
        PraetorDataLoaders(
            learning=run_praetor_learning_update,
            journal=run_praetor_journal_update,
            portfolio=get_portfolio_analysis,
            wealth=get_wealth_analysis,
        ),
    )


def get_notification_preferences(user_id: str) -> dict[str, Any]:
    sql = "SELECT * FROM notification_preferences WHERE user_id=%s"
    if using_postgres():
        with pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (user_id,))
                row = cur.fetchone()
                return dict(row) if row else default_notification_preferences(user_id)
    conn = sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute(sql.replace("%s", "?"), (user_id,))
        row = cur.fetchone()
        return dict(row) if row else default_notification_preferences(user_id)
    finally:
        conn.close()


def default_notification_preferences(user_id: str) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "alert_level": "high_and_critical",
        "cooldown_minutes": 20,
        "email_enabled": 0,
        "sms_enabled": 0,
        "push_enabled": 0,
        "quiet_hours_json": None,
        "updated_at_utc": datetime.utcnow().isoformat(),
    }


def save_notification_preferences(user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    prefs = default_notification_preferences(user_id)
    current = get_notification_preferences(user_id)
    prefs.update(current)
    if payload.get("alert_level") in ("critical", "high_and_critical", "all"):
        prefs["alert_level"] = payload["alert_level"]
    if payload.get("cooldown_minutes") is not None:
        prefs["cooldown_minutes"] = max(1, min(int(payload["cooldown_minutes"]), 1440))
    for key in ("email_enabled", "sms_enabled", "push_enabled"):
        if key in payload:
            prefs[key] = 1 if payload[key] else 0
    prefs["updated_at_utc"] = datetime.utcnow().isoformat()

    if using_postgres():
        with pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO notification_preferences (
                      user_id, alert_level, cooldown_minutes, email_enabled, sms_enabled, push_enabled, quiet_hours_json, updated_at_utc
                    ) VALUES (
                      %(user_id)s, %(alert_level)s, %(cooldown_minutes)s, %(email_enabled)s, %(sms_enabled)s, %(push_enabled)s, %(quiet_hours_json)s, %(updated_at_utc)s
                    )
                    ON CONFLICT (user_id) DO UPDATE SET
                      alert_level=EXCLUDED.alert_level,
                      cooldown_minutes=EXCLUDED.cooldown_minutes,
                      email_enabled=EXCLUDED.email_enabled,
                      sms_enabled=EXCLUDED.sms_enabled,
                      push_enabled=EXCLUDED.push_enabled,
                      quiet_hours_json=EXCLUDED.quiet_hours_json,
                      updated_at_utc=EXCLUDED.updated_at_utc
                    )
                    """,
                    prefs,
                )
            conn.commit()
        return prefs

    with DB_LOCK:
        conn = sqlite_conn()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO notification_preferences (
                  user_id, alert_level, cooldown_minutes, email_enabled, sms_enabled, push_enabled, quiet_hours_json, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    prefs["user_id"],
                    prefs["alert_level"],
                    prefs["cooldown_minutes"],
                    prefs["email_enabled"],
                    prefs["sms_enabled"],
                    prefs["push_enabled"],
                    prefs["quiet_hours_json"],
                    prefs["updated_at_utc"],
                ),
            )
            conn.commit()
        finally:
            conn.close()
    return prefs


def save_monitoring_run(user_id: str, health: dict[str, Any]) -> str:
    run_id = str(uuid.uuid4())
    row = {
        "id": run_id,
        "user_id": user_id,
        "active_plan_count": int(health.get("active_plan_count") or 0),
        "stale_plan_count": int(health.get("stale_plan_count") or 0),
        "open_alert_count": int(health.get("open_alert_count") or 0),
        "failed_check_count": int(health.get("failed_check_count") or 0),
        "health_json": json.dumps(health, default=str),
        "created_at_utc": datetime.utcnow().isoformat(),
    }
    if using_postgres():
        with pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO monitoring_runs (
                      id, user_id, active_plan_count, stale_plan_count, open_alert_count,
                      failed_check_count, health_json, created_at_utc
                    ) VALUES (
                      %(id)s, %(user_id)s, %(active_plan_count)s, %(stale_plan_count)s, %(open_alert_count)s,
                      %(failed_check_count)s, %(health_json)s, %(created_at_utc)s
                    )
                    """,
                    row,
                )
            conn.commit()
        return run_id

    with DB_LOCK:
        conn = sqlite_conn()
        try:
            conn.execute(
                """
                INSERT INTO monitoring_runs (
                  id, user_id, active_plan_count, stale_plan_count, open_alert_count,
                  failed_check_count, health_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row["user_id"],
                    row["active_plan_count"],
                    row["stale_plan_count"],
                    row["open_alert_count"],
                    row["failed_check_count"],
                    row["health_json"],
                    row["created_at_utc"],
                ),
            )
            conn.commit()
        finally:
            conn.close()
    return run_id


def run_praetor_learning_update(user_id: str) -> dict[str, Any]:
    plans = trade_plan_repo().list_plans(user_id, limit=1000)
    stats = calculate_playbook_stats(plans)
    snapshot_id = playbook_repo().save_snapshot(user_id, stats)

    memory_ids: list[str] = []
    for item in build_memory_updates(stats):
        memory_ids.append(memory_repo().upsert_memory(user_id, item))

    discovery_ids: list[str] = []
    for discovery in build_discovery_candidates(stats):
        discovery_ids.append(discovery_repo().save_discovery(user_id, discovery))

    memory = memory_repo().list_memory(user_id)
    discoveries = discovery_repo().list_discoveries(user_id)
    risk = build_risk_report(plans, stats, memory, discoveries)
    for discovery in build_discovery_v2_candidates(stats, risk_report=risk):
        discovery_ids.append(discovery_repo().save_discovery(user_id, discovery))
    discoveries = discovery_repo().list_discoveries(user_id)

    return {
        "ok": True,
        "snapshot_id": snapshot_id,
        "stats": stats,
        "memory_ids": memory_ids,
        "discovery_ids": discovery_ids,
        "risk": risk,
        "discovery_summary": summarize_discoveries(discoveries),
    }


def run_praetor_journal_update(user_id: str) -> dict[str, Any]:
    maybe_import_founder_past_trades(user_by_id(user_id) or {"id": user_id})
    trades = trades_select(view="all", user_id=user_id)
    plans = trade_plan_repo().list_plans(user_id, limit=1000)
    learning = run_praetor_learning_update(user_id)
    memory = memory_repo().list_memory(user_id)
    discoveries = discovery_repo().list_discoveries(user_id)
    journal_report = build_journal_report(trades, plans, learning["stats"], memory, discoveries, learning["risk"])

    memory_ids: list[str] = []
    for item in build_journal_memory_updates(journal_report):
        memory_ids.append(memory_repo().upsert_memory(user_id, item))

    discovery_ids: list[str] = []
    for discovery in build_journal_discovery_candidates(journal_report):
        discovery_ids.append(discovery_repo().save_discovery(user_id, discovery))
    for discovery in build_discovery_v2_candidates(learning["stats"], journal_report=journal_report, risk_report=learning["risk"]):
        discovery_ids.append(discovery_repo().save_discovery(user_id, discovery))

    return {
        "ok": True,
        "journal": journal_report,
        "memory_ids": memory_ids,
        "discovery_ids": discovery_ids,
        "memory": memory_repo().list_memory(user_id),
        "discoveries": discovery_repo().list_discoveries(user_id),
        "discovery_summary": summarize_discoveries(discovery_repo().list_discoveries(user_id)),
        "risk": learning["risk"],
    }


def run_praetor_monitoring(user_id: str, market_prices: dict[str, Any] | None = None) -> dict[str, Any]:
    prefs = get_notification_preferences(user_id)
    return praetor_orchestrator().monitoring_cycle(user_id, market_prices=market_prices or {**{}}, notification_preferences=prefs)


def build_briefing_context(user_id: str) -> dict[str, Any]:
    learning = run_praetor_learning_update(user_id)
    journal = run_praetor_journal_update(user_id)
    alerts = alert_repo().list_alerts(user_id, limit=100)
    discoveries = discovery_repo().list_discoveries(user_id, limit=100)
    plans = trade_plan_repo().list_plans(user_id, limit=1000)
    memory = memory_repo().list_memory(user_id, limit=100)
    research_reports = research_repo().list_reports(user_id, limit=10)
    return {
        "learning": learning,
        "journal": journal.get("journal"),
        "alerts": alerts,
        "discoveries": discoveries,
        "trade_plans": plans,
        "memory": memory,
        "risk": learning.get("risk"),
        "research_reports": research_reports,
    }


def generate_and_save_briefing(user_id: str, briefing_type: str) -> dict[str, Any]:
    return praetor_orchestrator().briefing(user_id, briefing_type)


def run_and_save_committee(user_id: str, committee_type: str = "general") -> dict[str, Any]:
    return praetor_orchestrator().committee(user_id, committee_type)


def get_portfolio_analysis(user_id: str) -> dict[str, Any]:
    portfolio = portfolio_repo().get_or_create_default_portfolio(user_id)
    holdings = portfolio_repo().list_holdings(user_id, portfolio_id=portfolio["id"])
    goals = portfolio.get("goals_json") if isinstance(portfolio.get("goals_json"), dict) else {}
    reports = research_repo().list_reports(user_id, limit=50)
    analysis = analyze_portfolio(holdings, goals=goals, research_reports=reports)
    portfolio_repo().save_snapshot(user_id, portfolio["id"], analysis)
    return {"ok": True, "portfolio": portfolio, "analysis": analysis}


def get_wealth_analysis(user_id: str, available_cash: float = 0, objective: str = "") -> dict[str, Any]:
    portfolio_result = get_portfolio_analysis(user_id)
    reports = research_repo().list_reports(user_id, limit=50)
    wealth = build_wealth_plan(
        portfolio_result.get("analysis") or {},
        research_reports=reports,
        available_cash=available_cash,
        objective=objective,
    )
    return {"ok": True, "wealth": wealth, "portfolio": portfolio_result.get("analysis"), "research_report_count": len(reports)}


def build_command_center_context(user_id: str) -> dict[str, Any]:
    orchestrator = praetor_orchestrator()
    data = orchestrator.build_context(user_id)
    if not data.get("portfolio"):
        data["portfolio"] = get_portfolio_analysis(user_id)["analysis"]
    if not data.get("wealth"):
        data["wealth"] = get_wealth_analysis(user_id)["wealth"]
    data["research_reports"] = research_repo().list_reports(user_id, limit=10)
    return {"sources": data, "command_center": build_command_center(data)}


# -------------------- Auth + plans --------------------
SESSION_COOKIE_NAME = "cp_session"
SESSION_DAYS = 30
PASSWORD_ITERATIONS = 260_000

PLANS: dict[str, dict[str, Any]] = {
    "founder": {
        "name": "Founder",
        "price": 0,
        "scan_limit_weekly": None,
        "ortex_limit_weekly": None,
        "features": [
            "Unlimited scans",
            "Unlimited ORTEX-backed scans",
            "Full scanner, journal, research, signals, risk, and future admin access",
        ],
        "public": False,
    },
    "free_trial": {
        "name": "Free Trial",
        "price": 0,
        "scan_limit_weekly": 1,
        "ortex_limit_weekly": 0,
        "lifetime_scan_limit": 1,
        "features": ["1 lifetime Polygon-only scan", "Public research previews"],
    },
    "starter": {
        "name": "Starter",
        "price": 29,
        "scan_limit_weekly": 5,
        "ortex_limit_weekly": 1,
        "features": ["Scanner access", "1 ORTEX-backed scan/week", "Trade journal"],
    },
    "trader_pro": {
        "name": "Trader Pro",
        "price": 79,
        "scan_limit_weekly": 25,
        "ortex_limit_weekly": 8,
        "features": ["Live scans", "Signals", "Alerts", "Journal analytics"],
    },
    "trader_elite": {
        "name": "Trader Elite",
        "price": 179,
        "scan_limit_weekly": 75,
        "ortex_limit_weekly": 25,
        "features": ["High-volume scanning", "Premium ORTEX allowance", "Performance analytics"],
    },
    "research_elite": {
        "name": "Research Elite",
        "price": 349,
        "scan_limit_weekly": 125,
        "ortex_limit_weekly": 50,
        "features": ["Institutional research suite", "Quant intelligence", "Risk dashboards"],
    },
}


def public_plans() -> list[dict[str, Any]]:
    return [{"code": code, **plan} for code, plan in PLANS.items() if plan.get("public", True)]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _owner_emails() -> set[str]:
    defaults = {"ryantallent8@gmail.com"}
    configured = {
        _normalize_email(x)
        for x in (os.getenv("OWNER_EMAILS") or "").split(",")
        if _normalize_email(x)
    }
    return defaults | configured


def _is_owner_email(email: str) -> bool:
    return _normalize_email(email) in _owner_emails()


def _is_valid_email(email: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email or ""))


def _hash_password(password: str, salt_b64: str | None = None) -> str:
    if salt_b64:
        salt = base64.b64decode(salt_b64.encode("ascii"))
    else:
        salt = secrets.token_bytes(16)
        salt_b64 = base64.b64encode(salt).decode("ascii")

    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    digest_b64 = base64.b64encode(digest).decode("ascii")
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt_b64}${digest_b64}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_b64, digest_b64 = stored.split("$", 3)
        if algo != "pbkdf2_sha256" or int(iterations) != PASSWORD_ITERATIONS:
            return False
        expected = _hash_password(password, salt_b64).split("$", 3)[3]
        return hmac.compare_digest(expected, digest_b64)
    except Exception:
        return False


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _cookie_secure() -> bool:
    if (os.getenv("COOKIE_SECURE") or "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    return (os.getenv("PUBLIC_BASE_URL") or "").strip().lower().startswith("https://")


def _week_start_ct_str(dt: datetime | None = None) -> str:
    dt = dt or now_ct()
    d = dt.date() - timedelta(days=dt.weekday())
    return d.strftime("%Y-%m-%d")


def _plan_for_user(user: dict[str, Any]) -> dict[str, Any]:
    code = (user.get("plan_code") or "free_trial").strip()
    plan = PLANS.get(code, PLANS["free_trial"])
    return {"code": code if code in PLANS else "free_trial", **plan}


def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    plan = _plan_for_user(user)
    return {
        "id": user.get("id"),
        "email": user.get("email"),
        "plan_code": plan["code"],
        "plan_name": plan["name"],
        "subscription_status": user.get("subscription_status") or "trial",
        "lifetime_scans_used": int(user.get("lifetime_scans_used") or 0),
    }


def update_user_plan(user_id: str, plan_code: str, subscription_status: str):
    if using_postgres():
        with pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET plan_code=%s, subscription_status=%s WHERE id=%s",
                    (plan_code, subscription_status, user_id),
                )
            conn.commit()
        return

    with DB_LOCK:
        conn = sqlite_conn()
        try:
            conn.execute(
                "UPDATE users SET plan_code=?, subscription_status=? WHERE id=?",
                (plan_code, subscription_status, user_id),
            )
            conn.commit()
        finally:
            conn.close()


def _ensure_owner_access(user: dict[str, Any] | None) -> dict[str, Any] | None:
    if not user:
        return None
    if not _is_owner_email(user.get("email") or ""):
        return user
    if user.get("plan_code") != "founder" or user.get("subscription_status") != "founder":
        update_user_plan(user["id"], "founder", "founder")
        user = dict(user)
        user["plan_code"] = "founder"
        user["subscription_status"] = "founder"
    return user


def promote_owner_accounts():
    emails = list(_owner_emails())
    if not emails:
        return

    if using_postgres():
        with pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET plan_code='founder', subscription_status='founder' WHERE email = ANY(%s)",
                    (emails,),
                )
            conn.commit()
        return

    with DB_LOCK:
        conn = sqlite_conn()
        try:
            conn.executemany(
                "UPDATE users SET plan_code='founder', subscription_status='founder' WHERE email=?",
                [(email,) for email in emails],
            )
            conn.commit()
        finally:
            conn.close()


def user_by_email(email: str) -> dict[str, Any] | None:
    email = _normalize_email(email)
    if using_postgres():
        with pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE email=%s", (email,))
                row = cur.fetchone()
                return _ensure_owner_access(dict(row) if row else None)

    conn = sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE email=?", (email,))
        row = cur.fetchone()
        return _ensure_owner_access(dict(row) if row else None)
    finally:
        conn.close()


def user_by_id(user_id: str) -> dict[str, Any] | None:
    if using_postgres():
        with pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE id=%s", (user_id,))
                row = cur.fetchone()
                return _ensure_owner_access(dict(row) if row else None)

    conn = sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE id=?", (user_id,))
        row = cur.fetchone()
        return _ensure_owner_access(dict(row) if row else None)
    finally:
        conn.close()


def create_user(email: str, password: str) -> dict[str, Any]:
    email = _normalize_email(email)
    is_owner = _is_owner_email(email)
    user = {
        "id": str(uuid.uuid4()),
        "email": email,
        "password_hash": _hash_password(password),
        "plan_code": "founder" if is_owner else "free_trial",
        "subscription_status": "founder" if is_owner else "trial",
        "lifetime_scans_used": 0,
        "created_at_utc": _utc_now_iso(),
    }

    if using_postgres():
        with pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users (
                      id, email, password_hash, plan_code, subscription_status,
                      lifetime_scans_used, created_at_utc
                    ) VALUES (
                      %(id)s, %(email)s, %(password_hash)s, %(plan_code)s,
                      %(subscription_status)s, %(lifetime_scans_used)s, %(created_at_utc)s
                    )
                    """,
                    user,
                )
            conn.commit()
        return user

    with DB_LOCK:
        conn = sqlite_conn()
        try:
            conn.execute(
                """
                INSERT INTO users (
                  id, email, password_hash, plan_code, subscription_status,
                  lifetime_scans_used, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user["id"],
                    user["email"],
                    user["password_hash"],
                    user["plan_code"],
                    user["subscription_status"],
                    user["lifetime_scans_used"],
                    user["created_at_utc"],
                ),
            )
            conn.commit()
        finally:
            conn.close()
    return user


def create_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    row = {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "token_hash": _hash_token(token),
        "created_at_utc": _utc_now_iso(),
        "expires_at_utc": (_utc_now() + timedelta(days=SESSION_DAYS)).isoformat(),
    }

    if using_postgres():
        with pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO sessions (
                      id, user_id, token_hash, created_at_utc, expires_at_utc
                    ) VALUES (
                      %(id)s, %(user_id)s, %(token_hash)s,
                      %(created_at_utc)s, %(expires_at_utc)s
                    )
                    """,
                    row,
                )
            conn.commit()
        return token

    with DB_LOCK:
        conn = sqlite_conn()
        try:
            conn.execute(
                """
                INSERT INTO sessions (
                  id, user_id, token_hash, created_at_utc, expires_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row["user_id"],
                    row["token_hash"],
                    row["created_at_utc"],
                    row["expires_at_utc"],
                ),
            )
            conn.commit()
        finally:
            conn.close()
    return token


def destroy_session(token: str | None):
    if not token:
        return
    token_hash = _hash_token(token)
    if using_postgres():
        with pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM sessions WHERE token_hash=%s", (token_hash,))
            conn.commit()
        return

    with DB_LOCK:
        conn = sqlite_conn()
        try:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
            conn.commit()
        finally:
            conn.close()


def current_user_from_request(request: Request) -> dict[str, Any] | None:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    token_hash = _hash_token(token)
    now_iso = _utc_now_iso()

    if using_postgres():
        with pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT u.*
                    FROM sessions s
                    JOIN users u ON u.id = s.user_id
                    WHERE s.token_hash=%s AND s.expires_at_utc > %s
                    """,
                    (token_hash, now_iso),
                )
                row = cur.fetchone()
                return _ensure_owner_access(dict(row) if row else None)

    conn = sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT u.*
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token_hash=? AND s.expires_at_utc > ?
            """,
            (token_hash, now_iso),
        )
        row = cur.fetchone()
        return _ensure_owner_access(dict(row) if row else None)
    finally:
        conn.close()


def require_user(request: Request) -> dict[str, Any] | JSONResponse:
    user = current_user_from_request(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Authentication required"}, status_code=401)
    return user


def get_usage(user_id: str) -> dict[str, Any]:
    week_start = _week_start_ct_str()
    if using_postgres():
        with pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM scan_usage WHERE user_id=%s AND week_start_ct=%s",
                    (user_id, week_start),
                )
                row = cur.fetchone()
                if row:
                    return dict(row)
    else:
        conn = sqlite_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM scan_usage WHERE user_id=? AND week_start_ct=?",
                (user_id, week_start),
            )
            row = cur.fetchone()
            if row:
                return dict(row)
        finally:
            conn.close()

    return {"user_id": user_id, "week_start_ct": week_start, "scans_used": 0, "ortex_scans_used": 0}


def usage_summary(user: dict[str, Any]) -> dict[str, Any]:
    plan = _plan_for_user(user)
    usage = get_usage(user["id"])
    lifetime_used = int(user.get("lifetime_scans_used") or 0)
    lifetime_limit = plan.get("lifetime_scan_limit")
    scan_limit_weekly = plan.get("scan_limit_weekly")
    ortex_limit_weekly = plan.get("ortex_limit_weekly")
    return {
        "plan": plan,
        "week_start_ct": usage["week_start_ct"],
        "scans_used": int(usage.get("scans_used") or 0),
        "scan_limit_weekly": int(scan_limit_weekly) if scan_limit_weekly is not None else None,
        "ortex_scans_used": int(usage.get("ortex_scans_used") or 0),
        "ortex_limit_weekly": int(ortex_limit_weekly) if ortex_limit_weekly is not None else None,
        "lifetime_scans_used": lifetime_used,
        "lifetime_scan_limit": lifetime_limit,
    }


def check_scan_allowed(user: dict[str, Any], wants_ortex: bool) -> tuple[bool, str | None]:
    summary = usage_summary(user)
    scan_limit = summary.get("scan_limit_weekly")
    if scan_limit is not None and summary["scans_used"] >= int(scan_limit):
        return False, "Weekly scan limit reached. Upgrade your plan or wait for the weekly reset."

    lifetime_limit = summary.get("lifetime_scan_limit")
    if lifetime_limit is not None and summary["lifetime_scans_used"] >= int(lifetime_limit):
        return False, "Free trial scan already used. Choose a paid plan to keep scanning."

    ortex_limit = summary.get("ortex_limit_weekly")
    if wants_ortex and ortex_limit is not None and summary["ortex_scans_used"] >= int(ortex_limit):
        return False, "Weekly ORTEX scan limit reached. Run a Polygon-only scan or upgrade your plan."

    return True, None


def increment_scan_usage(user_id: str, used_ortex: bool):
    week_start = _week_start_ct_str()
    if using_postgres():
        with pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO scan_usage (
                      user_id, week_start_ct, scans_used, ortex_scans_used
                    ) VALUES (
                      %s, %s, 1, %s
                    )
                    ON CONFLICT (user_id, week_start_ct)
                    DO UPDATE SET
                      scans_used = scan_usage.scans_used + 1,
                      ortex_scans_used = scan_usage.ortex_scans_used + EXCLUDED.ortex_scans_used
                    """,
                    (user_id, week_start, 1 if used_ortex else 0),
                )
                cur.execute(
                    "UPDATE users SET lifetime_scans_used = lifetime_scans_used + 1 WHERE id=%s",
                    (user_id,),
                )
            conn.commit()
        return

    with DB_LOCK:
        conn = sqlite_conn()
        try:
            conn.execute(
                """
                INSERT INTO scan_usage (
                  user_id, week_start_ct, scans_used, ortex_scans_used
                ) VALUES (?, ?, 1, ?)
                ON CONFLICT(user_id, week_start_ct)
                DO UPDATE SET
                  scans_used = scans_used + 1,
                  ortex_scans_used = ortex_scans_used + excluded.ortex_scans_used
                """,
                (user_id, week_start, 1 if used_ortex else 0),
            )
            conn.execute(
                "UPDATE users SET lifetime_scans_used = lifetime_scans_used + 1 WHERE id=?",
                (user_id,),
            )
            conn.commit()
        finally:
            conn.close()


def signals_insert_many(rows: list[dict]):
    if not rows:
        return

    if using_postgres():
        with pg_conn() as conn:
            with conn.cursor() as cur:
                for r in rows:
                    cur.execute(
                        """
                        INSERT INTO signals (
                          id, created_at_utc,
                          scan_id, scan_date_ct,
                          ticker, confidence,
                          entry, win_px, loss_px,
                          status
                        ) VALUES (
                          %(id)s, NOW(),
                          %(scan_id)s, %(scan_date_ct)s,
                          %(ticker)s, %(confidence)s,
                          %(entry)s, %(win_px)s, %(loss_px)s,
                          %(status)s
                        )
                        ON CONFLICT (scan_date_ct, ticker) DO NOTHING;
                        """,
                        r,
                    )
            conn.commit()
        return

    with DB_LOCK:
        conn = sqlite_conn()
        try:
            cur = conn.cursor()
            for r in rows:
                cur.execute(
                    """
                    INSERT OR IGNORE INTO signals (
                      id, created_at_utc,
                      scan_id, scan_date_ct,
                      ticker, confidence,
                      entry, win_px, loss_px,
                      status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        r["id"],
                        datetime.utcnow().isoformat(),
                        r.get("scan_id"),
                        r["scan_date_ct"],
                        r["ticker"],
                        r.get("confidence"),
                        r["entry"],
                        r["win_px"],
                        r["loss_px"],
                        r["status"],
                    ),
                )
            conn.commit()
        finally:
            conn.close()


def _ct_date_from_utc_ms(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    if CT_TZ:
        dt = dt.astimezone(CT_TZ)
    return dt.strftime("%Y-%m-%d")


def grade_yesterday_if_needed(log_fn: Optional[Callable[[str], None]] = None):
    today = ct_date()
    last_done = kv_get("last_graded_date_ct")
    if last_done == today:
        return

    yday = yesterday_ct_date()
    if log_fn:
        log_fn(f"[GRADE] Grading signals for {yday} (CT day 12–12)")

    # Pull all PENDING signals for yesterday
    if using_postgres():
        with pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM signals WHERE scan_date_ct=%s AND status='PENDING' LIMIT 5000",
                    (yday,),
                )
                sigs = [dict(r) for r in cur.fetchall()]
    else:
        conn = sqlite_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM signals WHERE scan_date_ct=? AND status='PENDING' LIMIT 5000",
                (yday,),
            )
            sigs = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    if not sigs:
        kv_set("last_graded_date_ct", today)
        if log_fn:
            log_fn(f"[GRADE] No pending signals found for {yday}. Marking graded.")
        return

    updates: list[tuple] = []
    for s in sigs:
        try:
            ticker = s["ticker"]
            entry = float(s["entry"])
            win_px = float(s["win_px"])
            loss_px = float(s["loss_px"])

            bars = scanner.get_minute_aggs(ticker, yday, log_fn=log_fn) or []
            bars = [b for b in bars if _ct_date_from_utc_ms(b["t"]) == yday]
            if not bars:
                continue

            trig_i = None
            for i, b in enumerate(bars):
                if float(b["h"]) >= entry:
                    trig_i = i
                    break

            if trig_i is None:
                updates.append((ticker, "NO_TRADE", None, None, None, None))
                continue

            triggered_at_utc = datetime.fromtimestamp(bars[trig_i]["t"] / 1000, tz=timezone.utc).isoformat()

            max_after = -1e18
            min_after = 1e18
            status = "NO_RESULT"
            resolved_at_utc = None

            for b in bars[trig_i:]:
                hi = float(b["h"])
                lo = float(b["l"])
                max_after = max(max_after, hi)
                min_after = min(min_after, lo)

                if hi >= win_px:
                    status = "WIN"
                    resolved_at_utc = datetime.fromtimestamp(b["t"] / 1000, tz=timezone.utc).isoformat()
                    break
                if lo <= loss_px:
                    status = "LOSS"
                    resolved_at_utc = datetime.fromtimestamp(b["t"] / 1000, tz=timezone.utc).isoformat()
                    break

            updates.append((ticker, status, triggered_at_utc, resolved_at_utc, max_after, min_after))
        except Exception:
            continue

    # Write updates
    if using_postgres():
        with pg_conn() as conn:
            with conn.cursor() as cur:
                for (ticker, status, trig_at, res_at, mx, mn) in updates:
                    cur.execute(
                        """
                        UPDATE signals
                        SET status=%s,
                            triggered_at_utc=%s,
                            resolved_at_utc=%s,
                            max_after_trigger=%s,
                            min_after_trigger=%s
                        WHERE scan_date_ct=%s AND ticker=%s AND status='PENDING'
                        """,
                        (status, trig_at, res_at, mx, mn, yday, ticker),
                    )
            conn.commit()
    else:
        with DB_LOCK:
            conn = sqlite_conn()
            try:
                cur = conn.cursor()
                for (ticker, status, trig_at, res_at, mx, mn) in updates:
                    cur.execute(
                        """
                        UPDATE signals
                        SET status=?,
                            triggered_at_utc=?,
                            resolved_at_utc=?,
                            max_after_trigger=?,
                            min_after_trigger=?
                        WHERE scan_date_ct=? AND ticker=? AND status='PENDING'
                        """,
                        (status, trig_at, res_at, mx, mn, yday, ticker),
                    )
                conn.commit()
            finally:
                conn.close()

    kv_set("last_graded_date_ct", today)
    if log_fn:
        log_fn(f"[GRADE] Done. Updated {len(updates)} signals for {yday}.")


def scoreboard_all_time() -> dict:
    if using_postgres():
        with pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status, entry, max_after_trigger, triggered_at_utc FROM signals")
                rows = [dict(r) for r in cur.fetchall()]
    else:
        conn = sqlite_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT status, entry, max_after_trigger, triggered_at_utc FROM signals")
            rows = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    counts = {"WIN": 0, "LOSS": 0, "NO_RESULT": 0, "NO_TRADE": 0, "PENDING": 0}
    max_gain_list: list[float] = []

    for r in rows:
        st = (r.get("status") or "PENDING").upper()
        if st not in counts:
            st = "PENDING"
        counts[st] += 1

        trig = r.get("triggered_at_utc")
        entry = r.get("entry")
        mx = r.get("max_after_trigger")
        if trig and entry is not None and mx is not None:
            try:
                ef = float(entry)
                mf = float(mx)
                if ef > 0:
                    max_gain_list.append((mf - ef) / ef)
            except Exception:
                pass

    wins = counts["WIN"]
    losses = counts["LOSS"]
    denom = wins + losses
    win_rate = (wins / denom) if denom > 0 else None
    avg_max_gain = (sum(max_gain_list) / len(max_gain_list)) if max_gain_list else None

    return {
        "ok": True,
        "wins": wins,
        "losses": losses,
        "no_result": counts["NO_RESULT"],
        "no_trade": counts["NO_TRADE"],
        "pending": counts["PENDING"],
        "win_rate_triggered": win_rate,
        "avg_max_gain_pct_triggered": avg_max_gain,
    }


# -------------------- Trades helpers --------------------
def trades_select(view: str, user_id: str | None) -> list[dict]:
    view = (view or "all").lower().strip()
    if view not in ("all", "yesterday"):
        view = "all"

    where = []
    params: list[Any] = []

    if user_id:
        where.append("user_id = %s")
        params.append(user_id)

    if view == "yesterday":
        where.append("(scan_date_ct = %s OR substr(created_at_utc,1,10) = %s)")
        y = yesterday_ct_date()
        params.append(y)
        params.append(y)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    sql = f"""
    SELECT
      id, user_id, scan_id, scan_date_ct,
      ticker, bucket, subtype, confidence, plan,
      trigger, stop, scan_close, move_pct, dollar_vol, range_pct, hold_pct, rel_vol, si_pct_ff, ctb, avail,
      entry_price, entry_time_ct, exit_price, exit_time_ct, shares,
      review_flags,
      review_text, reviewed_at_utc
    FROM trades
    {where_sql}
    ORDER BY created_at_utc DESC
    LIMIT 500;
    """

    if using_postgres():
        with pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = [dict(r) for r in cur.fetchall()]
    else:
        sql_sqlite = sql.replace("%s", "?")
        conn = sqlite_conn()
        try:
            cur = conn.cursor()
            cur.execute(sql_sqlite, params)
            rows = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    for r in rows:
        if r.get("review_flags") is None:
            r["review_flags"] = "[]"
        elif not isinstance(r["review_flags"], str):
            r["review_flags"] = json.dumps(r["review_flags"])

        ep = r.get("entry_price")
        xp = r.get("exit_price")
        sh = r.get("shares")

        if ep is None or xp is None or sh is None:
            r["pnl_dollars"] = None
            r["pnl_pct"] = None
        else:
            try:
                epf = float(ep)
                xpf = float(xp)
                shf = float(sh)
                r["pnl_dollars"] = (xpf - epf) * shf
                r["pnl_pct"] = (xpf - epf) / epf if epf != 0 else None
            except Exception:
                r["pnl_dollars"] = None
                r["pnl_pct"] = None

    return rows


def trade_insert(row: dict):
    if using_postgres():
        with pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO trades (
                      id, user_id, created_at_utc,
                      scan_id, scan_date_ct,
                      ticker, bucket, subtype, confidence, plan,
                      trigger, stop, scan_close, move_pct, dollar_vol, range_pct, hold_pct, rel_vol, si_pct_ff, ctb, avail,
                      entry_price, entry_time_ct, exit_price, exit_time_ct, shares, review_flags
                    ) VALUES (
                      %(id)s, %(user_id)s, %(created_at_utc)s,
                      %(scan_id)s, %(scan_date_ct)s,
                      %(ticker)s, %(bucket)s, %(subtype)s, %(confidence)s, %(plan)s,
                      %(trigger)s, %(stop)s, %(scan_close)s, %(move_pct)s, %(dollar_vol)s, %(range_pct)s, %(hold_pct)s, %(rel_vol)s, %(si_pct_ff)s, %(ctb)s, %(avail)s,
                      %(entry_price)s, %(entry_time_ct)s, %(exit_price)s, %(exit_time_ct)s, %(shares)s, %(review_flags)s
                    );
                    """,
                    row,
                )
            conn.commit()
        return

    with DB_LOCK:
        conn = sqlite_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO trades (
                  id, user_id, created_at_utc,
                  scan_id, scan_date_ct,
                  ticker, bucket, subtype, confidence, plan,
                  trigger, stop, scan_close, move_pct, dollar_vol, range_pct, hold_pct, rel_vol, si_pct_ff, ctb, avail,
                  entry_price, entry_time_ct, exit_price, exit_time_ct, shares, review_flags
                ) VALUES (
                  ?, ?, ?,
                  ?, ?,
                  ?, ?, ?, ?, ?,
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  ?, ?, ?, ?, ?, ?
                );
                """,
                (
                    row["id"],
                    row["user_id"],
                    row["created_at_utc"],
                    row.get("scan_id"),
                    row.get("scan_date_ct"),
                    row.get("ticker"),
                    row.get("bucket"),
                    row.get("subtype"),
                    row.get("confidence"),
                    row.get("plan"),
                    row.get("trigger"),
                    row.get("stop"),
                    row.get("scan_close"),
                    row.get("move_pct"),
                    row.get("dollar_vol"),
                    row.get("range_pct"),
                    row.get("hold_pct"),
                    row.get("rel_vol"),
                    row.get("si_pct_ff"),
                    row.get("ctb"),
                    row.get("avail"),
                    row.get("entry_price"),
                    row.get("entry_time_ct"),
                    row.get("exit_price"),
                    row.get("exit_time_ct"),
                    row.get("shares"),
                    row.get("review_flags"),
                ),
            )
            conn.commit()
        finally:
            conn.close()


PAST_TRADE_SEED: list[dict[str, Any]] = [
    {"date": "2026-01-22", "ticker": "NAMM", "entry_price": 2.16, "exit_price": 3.69, "shares": 46},
    {"date": "2026-01-22", "ticker": "MRNA", "entry_price": 49.53, "exit_price": 55.02, "shares": 3.5153},
    {"date": "2026-01-26", "ticker": "APLD", "entry_price": 38.22, "exit_price": 40.22, "shares": 7.553316},
    {"date": "2026-01-30", "ticker": "APRE", "entry_price": 0.86, "exit_price": 0.95, "shares": 116},
    {"date": "2026-02-03", "ticker": "LIMN", "entry_price": 0.90, "exit_price": 1.04, "shares": 120},
    {"date": "2026-02-03", "ticker": "LIMN", "entry_price": 1.24, "exit_price": 1.40, "shares": 200},
    {"date": "2026-02-03", "ticker": "LIMN", "entry_price": 1.61, "exit_price": 1.80, "shares": 160},
    {"date": "2026-02-09", "ticker": "UOKA", "entry_price": 2.13, "exit_price": 2.71, "shares": 46},
    {"date": "2026-02-10", "ticker": "QNCX", "entry_price": 0.45, "exit_price": 0.62, "shares": 336},
    {"date": "2026-02-12", "ticker": "CHOW", "entry_price": 0.81, "exit_price": 1.05, "shares": 287},
    {"date": "2026-02-26", "ticker": "AEHL", "entry_price": 1.24, "exit_price": 1.32, "shares": 310},
    {"date": "2026-01-23", "ticker": "MOVE", "entry_price": 21.90, "exit_price": 17.20, "shares": 16},
    {"date": "2026-02-03", "ticker": "FUSE", "entry_price": 2.95, "exit_price": 2.71, "shares": 134.896434},
    {"date": "2026-02-06", "ticker": "CCHH", "entry_price": 0.66, "exit_price": 0.59, "shares": 700},
    {"date": "2026-05-12", "ticker": "UBXG", "entry_price": 0.46, "exit_price": 0.34, "shares": 380},
]


def _past_trade_id(user_id: str, trade: dict[str, Any]) -> str:
    key = (
        f"cardo-past-trade:{user_id}:"
        f"{trade['date']}:{trade['ticker']}:{trade['entry_price']}:{trade['exit_price']}:{trade['shares']}"
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


def trade_exists(trade_id: str, user_id: str) -> bool:
    if using_postgres():
        with pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM trades WHERE id=%s AND user_id=%s", (trade_id, user_id))
                return cur.fetchone() is not None

    conn = sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM trades WHERE id=? AND user_id=?", (trade_id, user_id))
        return cur.fetchone() is not None
    finally:
        conn.close()


def past_trade_seed_summary() -> dict[str, Any]:
    winners = 0
    losers = 0
    total_pnl = 0.0
    rows: list[dict[str, Any]] = []
    for t in PAST_TRADE_SEED:
        pnl = (float(t["exit_price"]) - float(t["entry_price"])) * float(t["shares"])
        total_pnl += pnl
        winners += 1 if pnl > 0 else 0
        losers += 1 if pnl < 0 else 0
        rows.append({**t, "pnl_dollars": pnl, "pnl_pct": (float(t["exit_price"]) - float(t["entry_price"])) / float(t["entry_price"])})
    return {
        "count": len(PAST_TRADE_SEED),
        "winners": winners,
        "losers": losers,
        "total_pnl_dollars": total_pnl,
        "rows": rows,
    }


def import_past_trade_seed(user_id: str) -> dict[str, Any]:
    inserted = 0
    skipped = 0
    imported_ids: list[str] = []

    for t in PAST_TRADE_SEED:
        trade_id = _past_trade_id(user_id, t)
        if trade_exists(trade_id, user_id):
            skipped += 1
            continue

        pnl = (float(t["exit_price"]) - float(t["entry_price"])) * float(t["shares"])
        row = {
            "id": trade_id,
            "user_id": user_id,
            "created_at_utc": f"{t['date']}T16:00:00+00:00",
            "scan_id": None,
            "scan_date_ct": t["date"],
            "ticker": t["ticker"],
            "bucket": "PAST",
            "subtype": "imported past trade",
            "confidence": None,
            "plan": "Imported founder past trade. Original note: average entry and average exit supplied by Ryan.",
            "trigger": None,
            "stop": None,
            "scan_close": None,
            "move_pct": None,
            "dollar_vol": None,
            "range_pct": None,
            "hold_pct": None,
            "rel_vol": None,
            "si_pct_ff": None,
            "ctb": None,
            "avail": None,
            "entry_price": float(t["entry_price"]),
            "entry_time_ct": "Imported",
            "exit_price": float(t["exit_price"]),
            "exit_time_ct": "Imported",
            "shares": float(t["shares"]),
            "review_flags": json.dumps([{"icon": "I", "label": "Imported past trade"}]),
        }
        trade_insert(row)
        inserted += 1
        imported_ids.append(trade_id)

    summary = past_trade_seed_summary()
    return {
        "ok": True,
        "inserted": inserted,
        "skipped": skipped,
        "total_seed_trades": summary["count"],
        "seed_winners": summary["winners"],
        "seed_losers": summary["losers"],
        "seed_total_pnl_dollars": summary["total_pnl_dollars"],
        "imported_ids": imported_ids,
    }


def maybe_import_founder_past_trades(user: dict[str, Any] | None) -> dict[str, Any] | None:
    if not user or (user.get("plan_code") or "") != "founder":
        return None
    try:
        return import_past_trade_seed(user["id"])
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def trade_close(trade_id: str, exit_price: float, exit_time_ct: str, user_id: str | None = None):
    if using_postgres():
        with pg_conn() as conn:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        "UPDATE trades SET exit_price=%s, exit_time_ct=%s WHERE id=%s AND user_id=%s",
                        (exit_price, exit_time_ct, trade_id, user_id),
                    )
                else:
                    cur.execute(
                        "UPDATE trades SET exit_price=%s, exit_time_ct=%s WHERE id=%s",
                        (exit_price, exit_time_ct, trade_id),
                    )
            conn.commit()
        return

    with DB_LOCK:
        conn = sqlite_conn()
        try:
            cur = conn.cursor()
            if user_id:
                cur.execute(
                    "UPDATE trades SET exit_price=?, exit_time_ct=? WHERE id=? AND user_id=?",
                    (exit_price, exit_time_ct, trade_id, user_id),
                )
            else:
                cur.execute(
                    "UPDATE trades SET exit_price=?, exit_time_ct=? WHERE id=?",
                    (exit_price, exit_time_ct, trade_id),
                )
            conn.commit()
        finally:
            conn.close()


def trade_save_review(trade_id: str, review_text: str, user_id: str | None = None):
    reviewed_at_utc = datetime.utcnow().isoformat()

    if using_postgres():
        with pg_conn() as conn:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        "UPDATE trades SET review_text=%s, reviewed_at_utc=NOW() WHERE id=%s AND user_id=%s",
                        (review_text, trade_id, user_id),
                    )
                else:
                    cur.execute(
                        "UPDATE trades SET review_text=%s, reviewed_at_utc=NOW() WHERE id=%s",
                        (review_text, trade_id),
                    )
            conn.commit()
        return

    with DB_LOCK:
        conn = sqlite_conn()
        try:
            cur = conn.cursor()
            if user_id:
                cur.execute(
                    "UPDATE trades SET review_text=?, reviewed_at_utc=? WHERE id=? AND user_id=?",
                    (review_text, reviewed_at_utc, trade_id, user_id),
                )
            else:
                cur.execute(
                    "UPDATE trades SET review_text=?, reviewed_at_utc=? WHERE id=?",
                    (review_text, reviewed_at_utc, trade_id),
                )
            conn.commit()
        finally:
            conn.close()


def trade_save_flags(trade_id: str, review_flags: list[dict], user_id: str | None = None):
    if not review_flags:
        return
    flags_json = json.dumps(review_flags)

    if using_postgres():
        with pg_conn() as conn:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        "UPDATE trades SET review_flags=%s WHERE id=%s AND user_id=%s",
                        (flags_json, trade_id, user_id),
                    )
                else:
                    cur.execute("UPDATE trades SET review_flags=%s WHERE id=%s", (flags_json, trade_id))
            conn.commit()
        return

    with DB_LOCK:
        conn = sqlite_conn()
        try:
            cur = conn.cursor()
            if user_id:
                cur.execute(
                    "UPDATE trades SET review_flags=? WHERE id=? AND user_id=?",
                    (flags_json, trade_id, user_id),
                )
            else:
                cur.execute("UPDATE trades SET review_flags=? WHERE id=?", (flags_json, trade_id))
            conn.commit()
        finally:
            conn.close()


# -------------------- FastAPI app --------------------
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve UI + outputs
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/outputs", StaticFiles(directory=str(OUT_DIR)), name="outputs")

INDEX_PATH = STATIC_DIR / "index.html"


def _sha256(path: Path) -> str:
    try:
        b = path.read_bytes()
        return hashlib.sha256(b).hexdigest()[:12]
    except Exception:
        return "missing"


# -------------------- scan state (in-memory) --------------------
STATE_LOCK = threading.Lock()
STATE: dict[str, Any] = {
    "running": False,
    "scan_id": None,
    "started_at_ct": None,
    "logs": deque(maxlen=2500),
    "rows": deque(maxlen=400),
    "meta": {
        "mode": "auto",
        "ortex_requested": "off",
        "ortex_effective": "OFF",
        "window": "—",
        "date": None,
        "scanned_count": None,
    },
    "done": False,
    "ok": True,
    "html_path": None,
    "log_seq": 0,
    "row_seq": 0,
    "meta_seq": 0,
    "done_seq": 0,
}


def _state_snapshot() -> dict:
    with STATE_LOCK:
        return json.loads(
            json.dumps(
                {
                    "running": STATE["running"],
                    "scan_id": STATE["scan_id"],
                    "started_at_ct": STATE["started_at_ct"],
                    "meta": STATE["meta"],
                    "done": STATE["done"],
                    "ok": STATE["ok"],
                    "html_path": STATE["html_path"],
                    "log_seq": STATE["log_seq"],
                    "row_seq": STATE["row_seq"],
                    "meta_seq": STATE["meta_seq"],
                    "done_seq": STATE["done_seq"],
                }
            )
        )


def push_log(line: str):
    scanned = None
    window_name = None

    if "Snapshot tickers received:" in line:
        try:
            scanned = int(line.split("Snapshot tickers received:")[1].strip().split()[0])
        except Exception:
            scanned = None

    if line.startswith("Market window:"):
        try:
            window_name = line.split("Market window:")[1].strip().split(" (")[0].strip()
        except Exception:
            window_name = None

    with STATE_LOCK:
        STATE["logs"].append(line)
        STATE["log_seq"] += 1

        changed = False
        if scanned is not None and STATE["meta"].get("scanned_count") != scanned:
            STATE["meta"]["scanned_count"] = scanned
            changed = True
        if window_name and STATE["meta"].get("window") != window_name:
            STATE["meta"]["window"] = window_name
            changed = True

        if changed:
            STATE["meta_seq"] += 1


def push_row(row: dict):
    with STATE_LOCK:
        STATE["rows"].append(row)
        STATE["row_seq"] += 1


def mark_done(ok: bool, html_path: str | None):
    with STATE_LOCK:
        STATE["done"] = True
        STATE["ok"] = bool(ok)
        STATE["html_path"] = html_path
        STATE["running"] = False
        STATE["done_seq"] += 1


# -------------------- startup --------------------
@app.on_event("startup")
def _startup():
    db_init()
    promote_owner_accounts()


# -------------------- routes --------------------
@app.get("/")
def root():
    return FileResponse(
        str(INDEX_PATH),
        media_type="text/html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/ui")
def ui():
    return FileResponse(
        str(INDEX_PATH),
        media_type="text/html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/__ui_version")
def ui_version():
    return PlainTextResponse(f"index.html sha={_sha256(INDEX_PATH)}\n")


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)


@app.get("/health")
def health():
    snap = _state_snapshot()
    return {
        "ok": True,
        "running": snap["running"],
        "scan_id": snap["scan_id"],
        "meta": snap["meta"],
        "storage": "postgres" if using_postgres() else "sqlite",
        "database_url_set": bool(DATABASE_URL),
    }


@app.get("/debug_keys")
def debug_keys():
    return {
        "POLYGON_API_KEY_set": bool(os.getenv("POLYGON_API_KEY")),
        "ORTEX_API_KEY_set": bool(os.getenv("ORTEX_API_KEY")),
    }


@app.get("/api/fundamentals/status")
def api_fundamentals_status():
    fmp = fmp_status()
    sec = sec_status()
    return {
        "ok": True,
        "fmp_configured": fmp["configured"],
        "sec_available": sec["available"],
        "cache_enabled": True,
        "fmp_endpoints": [
            "/stable/income-statement",
            "/stable/balance-sheet-statement",
            "/stable/cash-flow-statement",
            "/stable/key-metrics",
            "/stable/ratios",
            "/stable/analyst-estimates",
            "/stable/stock-peers",
            "/stable/earnings",
            "/stable/key-metrics for each peer",
            "/stable/ratios for each peer",
        ],
        "providers": {
            "fmp": fmp,
            "sec": sec,
        },
        "ticker_normalization": {
            "examples": {
                "BRK.B": ticker_normalization_metadata("BRK.B"),
                "BF.B": ticker_normalization_metadata("BF.B"),
            }
        },
    }


@app.get("/api/fundamentals/debug/{ticker}")
def api_fundamentals_debug(ticker: str, request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    if (auth_user.get("plan_code") or "") != "founder":
        return JSONResponse({"ok": False, "error": "Founder access required"}, status_code=403)
    debug = fmp_provider().debug_bundle(ticker)
    analysis = build_fundamental_analysis(fmp_provider().fundamentals_bundle(ticker))
    return {"ok": True, "debug": debug, "analysis": analysis}


def _research_validation_row(row: dict[str, Any]) -> dict[str, Any]:
    report = row.get("report_json") or {}
    coverage = report.get("data_coverage") or {}
    peer = report.get("peer_benchmarking") or {}
    valuation = report.get("valuation") or {}
    conviction = report.get("conviction") or {}
    framework = report.get("sector_framework") or {}
    provider_status = coverage.get("provider_status") or {}
    return {
        "ticker": report.get("ticker") or row.get("ticker"),
        "created_at_utc": row.get("created_at_utc"),
        "verdict": report.get("verdict") or row.get("verdict"),
        "peer_count": coverage.get("peer_count") if coverage.get("peer_count") is not None else len(peer.get("peer_ranking") or []),
        "data_coverage_score": coverage.get("score"),
        "data_coverage_rating": coverage.get("rating"),
        "missing_data": coverage.get("missing_data") or report.get("data_gaps") or [],
        "provider_status": provider_status,
        "valuation_framework_used": framework.get("name") or "General Equity Framework",
        "valuation_framework_key": framework.get("key") or "general",
        "valuation_rating": valuation.get("rating"),
        "valuation_score": valuation.get("score"),
        "confidence_level": conviction.get("confidence"),
        "conviction_score": conviction.get("score"),
        "conviction_rating": conviction.get("rating"),
    }


@app.get("/api/research/validation-dashboard")
def api_research_validation_dashboard(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    if (auth_user.get("plan_code") or "") != "founder":
        return JSONResponse({"ok": False, "error": "Founder access required"}, status_code=403)
    reports = research_repo().list_reports(auth_user["id"], limit=50)
    rows = [_research_validation_row(row) for row in reports]
    return {
        "ok": True,
        "rows": rows,
        "frameworks": available_sector_frameworks(),
        "normalization_examples": {
            "BRK.B": ticker_normalization_metadata("BRK.B"),
            "BF.B": ticker_normalization_metadata("BF.B"),
        },
    }


@app.post("/clear_log")
def clear_log():
    with STATE_LOCK:
        STATE["logs"].clear()
        STATE["rows"].clear()
        STATE["log_seq"] += 1
        STATE["row_seq"] += 1
    return {"ok": True}


@app.post("/set_ortex")
def set_ortex(value: str = "off"):
    v = (value or "off").strip().lower()
    if v not in ("on", "off"):
        v = "off"
    with STATE_LOCK:
        STATE["meta"]["ortex_requested"] = v
        STATE["meta_seq"] += 1
    return {"ok": True, "ortex": v}


@app.post("/run_scan")
def run_scan(request: Request, mode: str = "auto", ortex: str = "off"):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    return _run_scan_for_user(auth_user, mode=mode, ortex=ortex)


def _run_scan_for_user(user: dict[str, Any] | None, mode: str = "auto", ortex: str = "off"):
    try:
        mode = (mode or "auto").strip().lower()
        ortex = (ortex or "off").strip().lower()

        if mode not in ("auto", "day", "night"):
            mode = "auto"
        if ortex not in ("on", "off"):
            ortex = "off"

        # grade yesterday once per CT day on first scan
        try:
            grade_yesterday_if_needed(log_fn=push_log)
        except Exception:
            pass

        dt = scanner.now_ct()
        window_name, w_start, _w_end = scanner.current_market_window(dt)
        date_str = scanner.ct_date_str(w_start)

        if mode == "auto":
            eff_mode = "day" if window_name in ("PREMARKET", "REGULAR", "AFTERHOURS") else "night"
        else:
            eff_mode = mode

        try:
            ortex_on, ortex_label = scanner.resolve_ortex_on(eff_mode, ortex, dt)
        except Exception:
            ortex_on, ortex_label = (False, "OFF (resolve err)")

        wants_ortex = ortex_on and ortex == "on"
        if user is not None:
            ok_allowed, limit_error = check_scan_allowed(user, wants_ortex=wants_ortex)
            if not ok_allowed:
                return JSONResponse({"ok": False, "error": limit_error, "usage": usage_summary(user)}, status_code=402)

        ortex_for_worker = "on" if ortex_on else "off"

        with STATE_LOCK:
            if STATE["running"]:
                return JSONResponse({"ok": False, "error": "Scan already running."}, status_code=409)

            scan_id = str(uuid.uuid4())

            STATE["running"] = True
            STATE["scan_id"] = scan_id
            STATE["done"] = False
            STATE["ok"] = True
            STATE["html_path"] = None

            STATE["logs"].clear()
            STATE["rows"].clear()
            STATE["log_seq"] += 1
            STATE["row_seq"] += 1
            STATE["meta_seq"] += 1
            STATE["done_seq"] += 1

            STATE["started_at_ct"] = now_ct_str()
            STATE["meta"] = {
                "mode": eff_mode,
                "ortex_requested": ortex,
                "ortex_effective": ortex_label,
                "window": window_name,
                "date": date_str,
                "scanned_count": None,
            }

        user_id = user["id"] if user is not None else None
        th = threading.Thread(
            target=_scan_worker,
            args=(scan_id, eff_mode, ortex_for_worker, user_id, wants_ortex),
            daemon=True,
        )
        th.start()

        snap = _state_snapshot()
        return {
            "ok": True,
            "scan_id": scan_id,
            "mode": snap["meta"]["mode"],
            "window": snap["meta"]["window"],
            "date": snap["meta"]["date"],
            "ortex_requested": snap["meta"]["ortex_requested"],
            "ortex_effective": snap["meta"]["ortex_effective"],
            "started_at_ct": snap["started_at_ct"],
            "scanned_count": snap["meta"]["scanned_count"],
        }

    except Exception as e:
        tb = traceback.format_exc()
        try:
            push_log(f"[RUN_SCAN ERROR] {type(e).__name__}: {str(e)}")
            push_log(tb)
        except Exception:
            pass
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {str(e)}"}, status_code=500)


def _scan_worker(
    scan_id: str,
    mode: str,
    ortex: str,
    user_id: str | None = None,
    used_ortex: bool = False,
):
    try:
        push_log("Starting scan… (manual)")
        push_log(f"Worker params → mode={mode} | ortex={ortex}")

        html_path: str | None = None
        picked_rows: list[dict] = []

        def _row_capture(r: dict):
            push_row(r)
            picked_rows.append(r)

        try:
            html_path = scanner.run_scan(
                log_fn=push_log,
                row_fn=_row_capture,
                mode=mode,
                ortex=ortex,
            )
        except Exception as scan_err:
            push_log(f"[SCANNER ERROR] {type(scan_err).__name__}: {str(scan_err)}")
            mark_done(False, None)
            return

        if user_id:
            try:
                increment_scan_usage(user_id, used_ortex=used_ortex)
                push_log("[USAGE] Scan credit recorded.")
            except Exception as usage_err:
                push_log(f"[USAGE WARNING] Could not record scan usage: {type(usage_err).__name__}")

        # Save only SQUEEZE picks as signals (dedupe by date+ticker)
        today_ct = ct_date()
        to_save: list[dict] = []

        for r in picked_rows:
            if (r.get("bucket") or "").upper() != "SQUEEZE":
                continue

            entry = r.get("trigger")
            try:
                entry = float(entry)
            except Exception:
                entry = 0.0
            if entry <= 0:
                continue

            stop_val = r.get("stop")
            stop_px = float(stop_val) if stop_val not in (None, "") else entry * 0.90
            risk = max(entry - stop_px, entry * 0.02)

            to_save.append(
                {
                    "id": str(uuid.uuid4()),
                    "scan_id": scan_id,
                    "scan_date_ct": today_ct,
                    "ticker": (r.get("ticker") or "").upper().strip(),
                    "confidence": r.get("confidence"),
                    "entry": entry,
                    "win_px": entry + (risk * 2.0),
                    "loss_px": stop_px,
                    "status": "PENDING",
                }
            )

        try:
            signals_insert_many(to_save)
            push_log(f"[SIGNALS] Saved {len(to_save)} squeeze signals (dedupe by date+ticker).")
        except Exception as e:
            push_log(f"[SIGNALS ERROR] {type(e).__name__}: {str(e)[:140]}")

        if html_path:
            push_log(f"Saved HTML: {html_path}")
        else:
            push_log("Scan completed. No candidates passed filters.")

        mark_done(True, html_path)

    except Exception as e:
        push_log(f"[FATAL WORKER ERROR] {type(e).__name__}: {str(e)}")
        mark_done(False, None)

        # Email alert (best-effort)
        try:
            base_url = (os.getenv("PUBLIC_BASE_URL") or "").strip()
            subject = f"SqueezeBot: {len(to_save)} squeeze signals • {today_ct} • {mode.upper()}"
            lines = [
                f"Scan date (CT): {today_ct}",
                f"Mode: {mode}",
                f"Ortex: {ortex}",
                f"Squeeze signals saved: {len(to_save)}",
                "",
                "Tickers:",
            ]
            for s in to_save:
                lines.append(f"- {s.get('ticker')} @ {s.get('entry')}  stop {s.get('loss_px')}  win {s.get('win_px')}")

            if base_url:
                lines += ["", f"Open app: {base_url}", f"Health: {base_url}/health"]

            alerts.send_email_resend(subject=subject, text="\n".join(lines))
        except Exception:
            pass

@app.post("/cron/run_scan")
def cron_run_scan(token: str, mode: str = "auto", ortex: str = "off"):
    expected = (os.getenv("CRON_TOKEN") or "").strip()
    if not expected or token != expected:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    return _run_scan_for_user(None, mode=mode, ortex=ortex)

@app.get("/stream/{scan_id}")
def stream(scan_id: str, request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user

    def event_gen() -> Generator[str, None, None]:
        last_log_seq = 0
        last_log_offset = 0
        last_row_seq = 0

        last_meta_seq = 0
        last_done_seq = 0

        snap = _state_snapshot()
        if snap["scan_id"] != scan_id:
            yield _sse("done", {"ok": False, "error": "Unknown scan_id"})
            return

        yield _sse("meta", snap["meta"])

        while True:
            snap = _state_snapshot()

            if snap["log_seq"] != last_log_seq:
                with STATE_LOCK:
                    logs = list(STATE["logs"])
                    seq = STATE["log_seq"]
                    new_lines = logs[last_log_offset:]
                    for line in new_lines:
                        yield _sse("log", {"line": line})
                    last_log_offset = len(logs)
                    last_log_seq = seq

            if snap["meta_seq"] != last_meta_seq:
                yield _sse("meta", snap["meta"])
                last_meta_seq = snap["meta_seq"]

            if snap["row_seq"] != last_row_seq:
                with STATE_LOCK:
                    rows = list(STATE["rows"])
                    seq = STATE["row_seq"]
                    for r in rows[-25:]:
                        yield _sse("row", r)
                    last_row_seq = seq

            if snap["done_seq"] != last_done_seq and snap["done"]:
                yield _sse("done", {"ok": snap["ok"], "html_path": snap["html_path"]})
                return

            yield ": ping\n\n"
            time.sleep(0.35)

    return _sse_response(event_gen)


def _sse(event_name: str, data_obj: dict) -> str:
    return f"event: {event_name}\ndata: {json.dumps(data_obj, ensure_ascii=False)}\n\n"


def _sse_response(gen_fn: Callable[[], Generator[str, None, None]]):
    from starlette.responses import StreamingResponse

    return StreamingResponse(gen_fn(), media_type="text/event-stream")


# -------------------- Research APIs/helpers --------------------
RESEARCH_EXPLANATIONS = {
    "open": "Opening price is where the stock started trading for that candle/day.",
    "high": "High is the highest traded price during the candle/day.",
    "low": "Low is the lowest traded price during the candle/day.",
    "close": "Close is the final traded price for the candle/day and is usually the main reference price.",
    "volume": "Volume is how many shares traded. Rising volume can confirm stronger institutional interest.",
    "pct_change": "Percent change shows how much the stock moved versus the previous close.",
    "sma20": "20-day moving average is a short-term trend line. Price above it often signals near-term strength.",
    "sma50": "50-day moving average is an intermediate trend line watched by many funds.",
    "sma200": "200-day moving average is a long-term trend line. Price above it usually means the long-term trend is healthier.",
    "volatility": "Volatility estimates how much the stock tends to move. Higher volatility means larger opportunity and larger risk.",
    "drawdown": "Drawdown measures how far price has fallen from a prior high. It helps identify risk and damage.",
    "relative_strength": "Relative strength compares this ticker's return with SPY over the same period.",
}


def _research_range_days(range_name: str) -> int:
    ranges = {"3m": 120, "6m": 210, "1y": 420, "2y": 760, "5y": 1900}
    return ranges.get((range_name or "1y").lower(), 420)


def _research_daily_aggs(ticker: str, range_name: str = "1y") -> list[dict[str, Any]]:
    provider_ticker = ticker_normalization_metadata(ticker)["providers"]["polygon"]
    end_dt = datetime.now(timezone.utc).date()
    start_dt = end_dt - timedelta(days=_research_range_days(range_name))
    url = (
        f"https://api.polygon.io/v2/aggs/ticker/{provider_ticker}"
        f"/range/1/day/{start_dt.isoformat()}/{end_dt.isoformat()}"
    )
    data = scanner.polygon_get(url, {"adjusted": "true", "sort": "asc", "limit": 50000})
    return data.get("results", []) or []


def _sma(values: list[float], idx: int, window: int) -> float | None:
    if idx + 1 < window:
        return None
    chunk = values[idx + 1 - window : idx + 1]
    return sum(chunk) / len(chunk)


def _max_drawdown(closes: list[float]) -> float | None:
    if not closes:
        return None
    peak = closes[0]
    worst = 0.0
    for c in closes:
        peak = max(peak, c)
        if peak > 0:
            worst = min(worst, (c - peak) / peak)
    return worst


def _annualized_volatility(returns: list[float]) -> float | None:
    clean = [r for r in returns if r is not None]
    if len(clean) < 2:
        return None
    return statistics.stdev(clean) * (252**0.5)


def _fmt_pct(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{x * 100:.1f}%"


def _fmt_money(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"${x:,.2f}"


def _research_note(close: float, sma20: float | None, sma50: float | None, sma200: float | None, pct_change: float | None) -> str:
    notes = []
    if pct_change is not None:
        if pct_change >= 0.05:
            notes.append("large upside day")
        elif pct_change <= -0.05:
            notes.append("large downside day")
    if sma20 is not None and close >= sma20:
        notes.append("above 20D trend")
    if sma50 is not None and close >= sma50:
        notes.append("above 50D trend")
    if sma200 is not None and close >= sma200:
        notes.append("above 200D trend")
    return "; ".join(notes) if notes else "neutral candle"


def build_research_profile(ticker: str, range_name: str = "1y") -> dict[str, Any]:
    ticker = (ticker or "").upper().strip()
    if not re.match(r"^[A-Z0-9.\-]{1,12}$", ticker):
        raise ValueError("Enter a valid ticker symbol.")

    bars = _research_daily_aggs(ticker, range_name)
    if len(bars) < 2:
        raise ValueError("Not enough price history returned for this ticker.")

    closes = [float(b["c"]) for b in bars]
    returns: list[float] = []
    chart: list[dict[str, Any]] = []
    running_high = closes[0]

    for i, b in enumerate(bars):
        close = float(b["c"])
        prev = closes[i - 1] if i > 0 else None
        pct_change = ((close - prev) / prev) if prev and prev > 0 else None
        if pct_change is not None:
            returns.append(pct_change)
        running_high = max(running_high, close)
        drawdown = ((close - running_high) / running_high) if running_high > 0 else None
        sma20 = _sma(closes, i, 20)
        sma50 = _sma(closes, i, 50)
        sma200 = _sma(closes, i, 200)
        dt = datetime.fromtimestamp(int(b["t"]) / 1000, tz=timezone.utc).date().isoformat()
        chart.append(
            {
                "date": dt,
                "open": float(b["o"]),
                "high": float(b["h"]),
                "low": float(b["l"]),
                "close": close,
                "volume": float(b.get("v") or 0),
                "pct_change": pct_change,
                "sma20": sma20,
                "sma50": sma50,
                "sma200": sma200,
                "drawdown": drawdown,
                "note": _research_note(close, sma20, sma50, sma200, pct_change),
            }
        )

    latest = chart[-1]
    first_close = closes[0]
    latest_close = closes[-1]
    period_return = ((latest_close - first_close) / first_close) if first_close > 0 else None
    avg_volume_20 = sum(float(b.get("v") or 0) for b in bars[-20:]) / min(len(bars), 20)
    high_52w = max(closes[-252:]) if closes else None
    low_52w = min(closes[-252:]) if closes else None

    relative_strength = None
    try:
        spy = _research_daily_aggs("SPY", range_name)
        if len(spy) >= 2:
            spy_ret = (float(spy[-1]["c"]) - float(spy[0]["c"])) / float(spy[0]["c"])
            if period_return is not None:
                relative_strength = period_return - spy_ret
    except Exception:
        relative_strength = None

    metrics = {
        "ticker": ticker,
        "range": range_name,
        "latest_close": latest_close,
        "period_return": period_return,
        "latest_pct_change": latest.get("pct_change"),
        "sma20": latest.get("sma20"),
        "sma50": latest.get("sma50"),
        "sma200": latest.get("sma200"),
        "annualized_volatility": _annualized_volatility(returns),
        "max_drawdown": _max_drawdown(closes),
        "latest_drawdown": latest.get("drawdown"),
        "avg_volume_20": avg_volume_20,
        "high_52w": high_52w,
        "low_52w": low_52w,
        "relative_strength_vs_spy": relative_strength,
    }

    return {
        "ok": True,
        "ticker": ticker,
        "ticker_normalization": ticker_normalization_metadata(ticker),
        "range": range_name,
        "chart": chart,
        "metrics": metrics,
        "explanations": RESEARCH_EXPLANATIONS,
    }


def generate_research_report(profile: dict[str, Any], report_type: str, objective: str = "", fundamentals: dict[str, Any] | None = None) -> dict[str, Any]:
    m = profile["metrics"]
    ticker = profile["ticker"]
    report_type = (report_type or "full").lower()
    objective = (objective or "User did not provide a specific objective.").strip()

    trend = "constructive" if (m.get("sma50") and m["latest_close"] >= m["sma50"]) else "fragile"
    long_trend = "above" if (m.get("sma200") and m["latest_close"] >= m["sma200"]) else "below or unavailable versus"
    vol = m.get("annualized_volatility")
    dd = m.get("max_drawdown")
    rs = m.get("relative_strength_vs_spy")

    sections = [
        {
            "title": "Executive view",
            "body": (
                f"{ticker} is trading at {_fmt_money(m.get('latest_close'))}. "
                f"The period return is {_fmt_pct(m.get('period_return'))}, with annualized volatility near {_fmt_pct(vol)}. "
                f"The intermediate trend currently looks {trend}, and price is {long_trend} the 200-day moving average."
            ),
        },
        {
            "title": "User objective fit",
            "body": f"Stated objective: {objective} The research lens should compare expected upside, volatility, liquidity, and drawdown tolerance against that objective.",
        },
        {
            "title": "Quantitative systems read",
            "body": (
                f"Relative strength versus SPY is {_fmt_pct(rs)} over the selected range. "
                f"Maximum drawdown in the range is {_fmt_pct(dd)}. "
                "A stronger quant profile would combine positive relative strength, price above the 50D/200D averages, controlled drawdown, and improving volume."
            ),
        },
        {
            "title": "Risk and exposure notes",
            "body": (
                f"Average 20-day volume is {m.get('avg_volume_20', 0):,.0f} shares. "
                "Position sizing should account for volatility, liquidity, gap risk, and whether this name increases concentration in an existing sector/theme."
            ),
        },
    ]

    if report_type == "quick":
        sections = sections[:3]
    elif report_type == "trading":
        sections.append(
            {
                "title": "Trading-focused interpretation",
                "body": "For active trading, prioritize current trend alignment, volume confirmation, stop placement below a defensible technical level, and avoiding oversized exposure into high volatility.",
            }
        )
    elif report_type == "investor":
        sections.append(
            {
                "title": "Investor-focused interpretation",
                "body": "For longer-term investing, this price/technical profile should be paired with fundamental work: revenue durability, margin quality, free cash flow, balance sheet strength, valuation, and sector comparison.",
            }
        )
    else:
        sections.extend(
            [
                {
                    "title": "Fundamental research checklist",
                    "body": "Next data layer: DCF assumptions, revenue decomposition, earnings quality, free cash flow, margin trend, balance sheet stress, guidance revisions, and sector benchmarking.",
                },
                {
                    "title": "Alternative data checklist",
                    "body": "Next data layer: options flow, dark-pool activity, insider accumulation, sentiment, macro correlations, volatility flow, supply-chain signals, and liquidity flows.",
                },
            ]
        )

    plain_english = [
        "Moving averages help show whether buyers are supporting price over short, medium, and long timeframes.",
        "Volatility tells you how violently the stock can move; high volatility can help returns but also increases position-size risk.",
        "Drawdown tells you how much damage happened from a prior high; deep drawdowns need smaller sizing or stronger thesis evidence.",
        "Relative strength shows whether this stock is outperforming or lagging the broad market.",
    ]

    base = {"type": report_type, "sections": sections, "plain_english": plain_english}
    return {**base, "institutional": build_institutional_research(profile, base, objective, fundamentals=fundamentals)}


def build_portfolio_plan(payload: dict[str, Any]) -> dict[str, Any]:
    capital = float(payload.get("capital") or 0)
    if capital <= 0:
        raise ValueError("Capital must be greater than 0.")
    objective = (payload.get("objective") or "").strip() or "No objective provided."
    risk = (payload.get("risk_tolerance") or "moderate").lower()
    horizon = (payload.get("time_horizon") or "").strip() or "Not specified"
    max_position_pct = max(1.0, min(float(payload.get("max_position_pct") or 10), 50.0))
    cash_reserve_pct = max(0.0, min(float(payload.get("cash_reserve_pct") or 10), 80.0))
    preference = (payload.get("trading_preference") or "mixed").lower()
    prefer = (payload.get("sectors_prefer") or "").strip()
    avoid = (payload.get("sectors_avoid") or "").strip()
    holdings = (payload.get("current_holdings") or "").strip()

    cash = capital * (cash_reserve_pct / 100)
    deployable = capital - cash
    max_position = capital * (max_position_pct / 100)

    if risk in ("aggressive", "high"):
        core, tactical, alternatives = 0.45, 0.40, 0.15
    elif risk in ("conservative", "low"):
        core, tactical, alternatives = 0.70, 0.15, 0.15
    else:
        core, tactical, alternatives = 0.58, 0.27, 0.15

    if "trading" in preference or "short" in preference:
        tactical += 0.10
        core -= 0.10
    elif "invest" in preference or "long" in preference:
        core += 0.10
        tactical -= 0.10

    buckets = [
        {"bucket": "Core compounders / broad exposure", "weight": core, "dollars": deployable * core},
        {"bucket": "Active research ideas / tactical opportunities", "weight": tactical, "dollars": deployable * tactical},
        {"bucket": "Hedges, cash-like assets, or diversifiers", "weight": alternatives, "dollars": deployable * alternatives},
        {"bucket": "Cash reserve", "weight": cash_reserve_pct / 100, "dollars": cash},
    ]

    guardrails = [
        f"Maximum single position: {max_position_pct:.1f}% or about {_fmt_money(max_position)}.",
        f"Cash reserve target: {cash_reserve_pct:.1f}% or about {_fmt_money(cash)}.",
        "Scale into positions rather than deploying all capital at one price when volatility is elevated.",
        "Review sector and factor concentration before adding new names.",
    ]
    if prefer:
        guardrails.append(f"Preferred sectors/themes to prioritize: {prefer}.")
    if avoid:
        guardrails.append(f"Sectors/themes to avoid or cap: {avoid}.")
    if holdings:
        guardrails.append(f"Current holdings to incorporate into exposure review: {holdings}.")

    return {
        "ok": True,
        "capital": capital,
        "objective": objective,
        "time_horizon": horizon,
        "risk_tolerance": risk,
        "trading_preference": preference,
        "deployable_capital": deployable,
        "max_position_dollars": max_position,
        "buckets": buckets,
        "guardrails": guardrails,
        "next_steps": [
            "Generate research reports for candidate tickers.",
            "Compare each candidate against the objective, risk tolerance, and max-position rule.",
            "Build a watchlist ranked by thesis strength, relative strength, liquidity, volatility, and drawdown risk.",
        ],
    }


@app.get("/api/research/{ticker}")
def api_research_ticker(ticker: str, request: Request, range: str = "1y"):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    try:
        return build_research_profile(ticker, range)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:240]}, status_code=500)


@app.post("/api/research/report")
async def api_research_report(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    payload = await request.json()
    ticker = payload.get("ticker") or ""
    range_name = payload.get("range") or "1y"
    report_type = payload.get("report_type") or "full"
    objective = payload.get("objective") or ""
    try:
        profile = build_research_profile(ticker, range_name)
        fundamentals = get_fundamental_analysis(ticker, profile=profile)
        report = generate_research_report(profile, report_type, objective, fundamentals=fundamentals)
        research_repo().save_report(auth_user["id"], report.get("institutional") or {}, profile=profile)
        return {"ok": True, "report": report}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:240]}, status_code=500)


@app.post("/api/praetor/ai/research")
async def api_praetor_ai_research(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    payload = await request.json()
    ticker = payload.get("ticker") or ""
    range_name = payload.get("range") or "1y"
    report_type = payload.get("report_type") or "full"
    objective = payload.get("objective") or ""
    try:
        profile = build_research_profile(ticker, range_name)
        fundamentals = get_fundamental_analysis(ticker, profile=profile)
        deterministic_report = generate_research_report(profile, report_type, objective, fundamentals=fundamentals)
        institutional = deterministic_report.get("institutional") or {}
        ai = synthesize_ai(
            "research",
            {
                "ticker": ticker,
                "objective": objective,
                "profile": profile,
                "deterministic_report": deterministic_report,
                "institutional_report": institutional,
                "fundamentals": fundamentals,
                "user": _public_user(auth_user),
            },
        )
        research_repo().save_report(auth_user["id"], institutional, profile=profile, ai=ai)
        return {
            "ok": True,
            "profile": profile,
            "deterministic_report": deterministic_report,
            "institutional_report": institutional,
            "ai": ai,
        }
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:240]}, status_code=500)


@app.post("/api/research/portfolio")
async def api_research_portfolio(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    payload = await request.json()
    try:
        return build_portfolio_plan(payload)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:240]}, status_code=400)


@app.get("/api/portfolio")
def api_get_portfolio(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    try:
        return get_portfolio_analysis(auth_user["id"])
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:240]}, status_code=500)


@app.get("/api/wealth")
def api_get_wealth(request: Request, available_cash: float = 0, objective: str = ""):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    try:
        return get_wealth_analysis(auth_user["id"], available_cash=available_cash, objective=objective)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:240]}, status_code=500)


@app.post("/api/wealth/allocate")
async def api_wealth_allocate(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    payload = await request.json()
    try:
        return get_wealth_analysis(auth_user["id"], available_cash=float(payload.get("available_cash") or 0), objective=payload.get("objective") or "")
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:240]}, status_code=400)


@app.post("/api/portfolio/holdings")
async def api_upsert_portfolio_holding(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    payload = await request.json()
    try:
        portfolio = portfolio_repo().get_or_create_default_portfolio(auth_user["id"])
        holding_id = portfolio_repo().upsert_holding(auth_user["id"], portfolio["id"], payload)
        return {"ok": True, "id": holding_id, **get_portfolio_analysis(auth_user["id"])}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:240]}, status_code=400)


# -------------------- Auth APIs --------------------
@app.get("/api/plans")
def api_plans():
    return {"ok": True, "plans": public_plans()}


@app.get("/api/auth/me")
def api_auth_me(request: Request):
    user = current_user_from_request(request)
    if not user:
        return {"ok": True, "user": None, "usage": None, "plans": public_plans()}
    past_trades_seed = maybe_import_founder_past_trades(user)
    return {
        "ok": True,
        "user": _public_user(user),
        "usage": usage_summary(user),
        "plans": public_plans(),
        "past_trades_seed": past_trades_seed,
    }


@app.post("/api/auth/signup")
async def api_auth_signup(request: Request):
    payload = await request.json()
    email = _normalize_email(payload.get("email") or "")
    password = payload.get("password") or ""

    if not _is_valid_email(email):
        return JSONResponse({"ok": False, "error": "Enter a valid email address."}, status_code=400)
    if len(password) < 8:
        return JSONResponse({"ok": False, "error": "Password must be at least 8 characters."}, status_code=400)
    if user_by_email(email):
        return JSONResponse({"ok": False, "error": "An account already exists for that email."}, status_code=409)

    try:
        user = create_user(email, password)
        past_trades_seed = maybe_import_founder_past_trades(user)
        token = create_session(user["id"])
        resp = JSONResponse(
            {
                "ok": True,
                "user": _public_user(user),
                "usage": usage_summary(user),
                "plans": public_plans(),
                "past_trades_seed": past_trades_seed,
            }
        )
        resp.set_cookie(
            SESSION_COOKIE_NAME,
            token,
            max_age=SESSION_DAYS * 24 * 60 * 60,
            httponly=True,
            secure=_cookie_secure(),
            samesite="lax",
        )
        return resp
    except sqlite3.IntegrityError:
        return JSONResponse({"ok": False, "error": "An account already exists for that email."}, status_code=409)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@app.post("/api/auth/login")
async def api_auth_login(request: Request):
    payload = await request.json()
    email = _normalize_email(payload.get("email") or "")
    password = payload.get("password") or ""

    user = user_by_email(email)
    if not user or not _verify_password(password, user.get("password_hash") or ""):
        return JSONResponse({"ok": False, "error": "Invalid email or password."}, status_code=401)

    token = create_session(user["id"])
    past_trades_seed = maybe_import_founder_past_trades(user)
    resp = JSONResponse(
        {
            "ok": True,
            "user": _public_user(user),
            "usage": usage_summary(user),
            "plans": public_plans(),
            "past_trades_seed": past_trades_seed,
        }
    )
    resp.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=SESSION_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
    )
    return resp


@app.post("/api/auth/logout")
def api_auth_logout(request: Request):
    destroy_session(request.cookies.get(SESSION_COOKIE_NAME))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE_NAME)
    return resp


@app.get("/api/usage")
def api_usage(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    return {"ok": True, "usage": usage_summary(auth_user), "user": _public_user(auth_user)}


@app.get("/api/debug/ortex/{ticker}")
def api_debug_ortex(ticker: str, request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    if (auth_user.get("plan_code") or "") != "founder":
        return JSONResponse({"ok": False, "error": "Founder access required"}, status_code=403)

    try:
        return scanner.ortex_debug_ticker(ticker)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@app.post("/api/praetor/scanner/ask")
async def api_praetor_scanner_ask(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    payload = await request.json()
    scanner_row = payload.get("scanner_row") or {}
    question = (payload.get("question") or "Give me your scanner assessment.").strip()
    context = build_scanner_context(auth_user, scanner_row)
    service = PraetorService()
    result = service.ask(question, context)
    result_dict = response_to_dict(result)
    try:
        interaction_id = praetor_log_interaction(
            user_id=auth_user["id"],
            page_context="scanner",
            module="scanner_ai",
            topic=(scanner_row.get("ticker") or "scanner"),
            user_message=question,
            praetor_response=result.response,
            context=context.to_dict(),
            tools_used=["scanner_row"],
        )
        result_dict["interaction_id"] = interaction_id
    except Exception:
        pass
    return result_dict


@app.post("/api/praetor/ask")
async def api_praetor_ask(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    payload = await request.json()
    page = payload.get("page") or "global"
    question = (payload.get("question") or "").strip()
    scanner_row = payload.get("scanner_row")
    context = build_scanner_context(auth_user, scanner_row or {}) if scanner_row else build_scanner_context(auth_user, {})
    context.page = page
    context.module = "global_praetor"
    context.extra["client_context"] = payload.get("client_context") or {}
    if page == "command_center":
        context.extra["command_center"] = build_command_center_context(auth_user["id"])["command_center"]
    if page == "portfolio":
        context.extra["portfolio"] = get_portfolio_analysis(auth_user["id"])["analysis"]
    if page == "wealth":
        context.extra["wealth"] = get_wealth_analysis(auth_user["id"])["wealth"]
    service = PraetorService()
    result = service.ask(question or "Help me understand this page.", context)
    return response_to_dict(result)


@app.post("/api/praetor/trade-plan")
async def api_praetor_trade_plan(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    payload = await request.json()
    scanner_row = payload.get("scanner_row") or {}
    style = payload.get("style") or "balanced"
    service = PraetorService()
    result = service.scanner_trade_plan(scanner_row, style=style)
    result_dict = response_to_dict(result)
    plans = (result.structured or {}).get("trade_plans") or []
    saved_ids: list[str] = []
    for plan in plans:
        try:
            saved_ids.append(trade_plan_repo().save_plan(auth_user["id"], plan, scanner_row=scanner_row))
        except Exception as e:
            result_dict["save_error"] = str(e)[:200]
    result_dict["trade_plan_ids"] = saved_ids
    if saved_ids:
        result_dict["trade_plan_id"] = saved_ids[0]
    return result_dict


@app.get("/api/praetor/playbook")
def api_praetor_playbook(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    service = PraetorService()
    return response_to_dict(service.playbook_summary())


@app.get("/api/praetor/playbook/learning")
def api_praetor_playbook_learning(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    learning = run_praetor_learning_update(auth_user["id"])
    return {
        "ok": True,
        "learning": learning,
        "memory": memory_repo().list_memory(auth_user["id"]),
        "discoveries": discovery_repo().list_discoveries(auth_user["id"]),
    }


@app.get("/api/praetor/ai/status")
def api_praetor_ai_status(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    return ai_provider_status()


@app.get("/api/praetor/command-center")
def api_praetor_command_center(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    try:
        return build_command_center_context(auth_user["id"])["command_center"]
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:240]}, status_code=500)


@app.get("/api/praetor/ai/command-center")
def api_praetor_ai_command_center(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    try:
        context = build_command_center_context(auth_user["id"])
        return {"ok": True, **context, "ai": synthesize_ai("command_center", context)}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:240]}, status_code=500)


@app.get("/api/praetor/risk")
def api_praetor_risk(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    learning = run_praetor_learning_update(auth_user["id"])
    return {"ok": True, "risk": learning.get("risk"), "learning": learning}


@app.get("/api/praetor/discoveries")
def api_praetor_discoveries(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    learning = run_praetor_learning_update(auth_user["id"])
    discoveries = discovery_repo().list_discoveries(auth_user["id"], limit=100)
    return {"ok": True, "discoveries": discoveries, "summary": summarize_discoveries(discoveries), "learning": learning}


@app.get("/api/praetor/briefings")
def api_praetor_briefings(request: Request, briefing_type: str | None = None):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    return {"ok": True, "briefings": briefing_repo().list_briefings(auth_user["id"], briefing_type=briefing_type)}


@app.post("/api/praetor/briefings/generate")
async def api_praetor_generate_briefing(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    payload = await request.json()
    try:
        return generate_and_save_briefing(auth_user["id"], payload.get("briefing_type") or "morning")
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:240]}, status_code=500)


@app.get("/api/praetor/committee")
def api_praetor_committee(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    return {"ok": True, "committee_runs": committee_repo().list_runs(auth_user["id"])}


@app.post("/api/praetor/committee/run")
async def api_praetor_committee_run(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    payload = await request.json()
    try:
        return run_and_save_committee(auth_user["id"], committee_type=payload.get("committee_type") or "general")
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:240]}, status_code=500)


@app.post("/api/praetor/ai/committee")
async def api_praetor_ai_committee(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    payload = await request.json()
    committee_result = run_and_save_committee(auth_user["id"], committee_type=payload.get("committee_type") or "general")
    return {"ok": True, **committee_result, "ai": synthesize_ai("committee", committee_result)}


@app.get("/api/praetor/ai/committee/latest")
def api_praetor_ai_committee_latest(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    runs = committee_repo().list_runs(auth_user["id"], limit=1)
    if not runs:
        return {"ok": True, "ai": synthesize_ai("committee", {"committee": {"synthesis": {"consensus": "n/a", "final_recommendation": "No committee run yet."}}})}
    run = runs[0]
    committee = {"votes": run.get("votes_json") or [], "synthesis": run.get("synthesis_json") or {}}
    return {"ok": True, "committee": committee, "ai": synthesize_ai("committee", {"committee": committee})}


@app.get("/api/praetor/journal/learning")
def api_praetor_journal_learning(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    try:
        return run_praetor_journal_update(auth_user["id"])
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:240]}, status_code=500)


@app.get("/api/praetor/ai/risk")
def api_praetor_ai_risk(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    learning = run_praetor_learning_update(auth_user["id"])
    return {"ok": True, "risk": learning.get("risk"), "ai": synthesize_ai("risk", {"risk": learning.get("risk"), "learning": learning})}


@app.get("/api/praetor/ai/journal")
def api_praetor_ai_journal(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    journal = run_praetor_journal_update(auth_user["id"])
    return {"ok": True, **journal, "ai": synthesize_ai("journal", journal)}


@app.post("/api/praetor/ai/briefing")
async def api_praetor_ai_briefing(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    payload = await request.json()
    briefing = generate_and_save_briefing(auth_user["id"], payload.get("briefing_type") or "morning")
    return {"ok": True, **briefing, "ai": synthesize_ai("briefing", {"briefing": briefing.get("briefing")})}


@app.get("/api/praetor/ai/briefing/latest")
def api_praetor_ai_briefing_latest(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    runs = briefing_repo().list_briefings(auth_user["id"], limit=1)
    if not runs:
        return {"ok": True, "ai": synthesize_ai("briefing", {"briefing": {"title": "Briefing", "lead": "No briefing generated yet."}})}
    briefing = runs[0].get("content_json") or {}
    return {"ok": True, "briefing": briefing, "ai": synthesize_ai("briefing", {"briefing": briefing})}


@app.get("/api/praetor/trade-plans")
def api_praetor_trade_plans(request: Request, status: str | None = None):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    try:
        return {"ok": True, "trade_plans": trade_plan_repo().list_plans(auth_user["id"], status=status)}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@app.patch("/api/praetor/trade-plans/{plan_id}/decision")
async def api_praetor_trade_plan_decision(plan_id: str, request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    payload = await request.json()
    try:
        updated = trade_plan_repo().update_decision(
            auth_user["id"],
            plan_id,
            payload.get("decision_status") or "",
            payload.get("notes") or "",
        )
        return {"ok": bool(updated)}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=400)


@app.patch("/api/praetor/trade-plans/{plan_id}/outcome")
async def api_praetor_trade_plan_outcome(plan_id: str, request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    payload = await request.json()
    try:
        updated = trade_plan_repo().update_outcome(
            auth_user["id"],
            plan_id,
            payload.get("outcome") or "",
            payload.get("notes") or "",
        )
        learning = run_praetor_learning_update(auth_user["id"]) if updated else None
        return {"ok": bool(updated), "learning": learning}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=400)


@app.get("/api/praetor/alerts")
def api_praetor_alerts(request: Request, status: str | None = None):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    return {"ok": True, "alerts": alert_repo().list_alerts(auth_user["id"], status=status)}


@app.post("/api/praetor/monitor/run")
async def api_praetor_monitor_run(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    payload = await request.json()
    try:
        return run_praetor_monitoring(auth_user["id"], market_prices=payload.get("market_prices") or {})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:240]}, status_code=400)


@app.get("/api/praetor/monitor/health")
def api_praetor_monitor_health(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    try:
        health = praetor_orchestrator().monitoring_health(auth_user["id"])
        run_id = save_monitoring_run(auth_user["id"], health)
        return {"ok": True, "health": health, "monitoring_run_id": run_id}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:240]}, status_code=500)


@app.post("/api/praetor/monitor/run-v2")
async def api_praetor_monitor_run_v2(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    payload = await request.json()
    try:
        result = run_praetor_monitoring(auth_user["id"], market_prices=payload.get("market_prices") or {})
        run_id = save_monitoring_run(auth_user["id"], result.get("health") or {})
        result["monitoring_run_id"] = run_id
        return result
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:240]}, status_code=400)


@app.get("/api/praetor/notification-preferences")
def api_praetor_notification_preferences(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    return {"ok": True, "preferences": get_notification_preferences(auth_user["id"])}


@app.patch("/api/praetor/notification-preferences")
async def api_praetor_update_notification_preferences(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    payload = await request.json()
    try:
        return {"ok": True, "preferences": save_notification_preferences(auth_user["id"], payload)}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:240]}, status_code=400)


@app.post("/api/praetor/alerts")
async def api_praetor_create_alert(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    payload = await request.json()
    try:
        alert_id = alert_repo().create_alert(
            user_id=auth_user["id"],
            alert_type=payload.get("alert_type") or "Manual Alert",
            ticker=payload.get("ticker") or "",
            message=payload.get("message") or "",
            urgency=payload.get("urgency") or "normal",
            importance=payload.get("importance") or "watchlist",
            confidence=payload.get("confidence"),
            related_entity_type=payload.get("related_entity_type"),
            related_entity_id=payload.get("related_entity_id"),
            evidence=payload.get("evidence") or {},
            category=payload.get("category"),
            priority=payload.get("priority"),
            source_modules=payload.get("source_modules") or [],
            explanation=payload.get("explanation"),
        )
        return {"ok": True, "id": alert_id}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=400)


@app.patch("/api/praetor/alerts/{alert_id}")
async def api_praetor_update_alert(alert_id: str, request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    payload = await request.json()
    try:
        updated = alert_repo().update_status(auth_user["id"], alert_id, payload.get("status") or "")
        return {"ok": bool(updated)}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=400)


@app.get("/api/founder/past-trades/seed")
def api_founder_past_trades_seed(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    if (auth_user.get("plan_code") or "") != "founder":
        return JSONResponse({"ok": False, "error": "Founder access required"}, status_code=403)
    return {"ok": True, "seed": past_trade_seed_summary()}


@app.post("/api/founder/past-trades/import")
def api_founder_import_past_trades(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    if (auth_user.get("plan_code") or "") != "founder":
        return JSONResponse({"ok": False, "error": "Founder access required"}, status_code=403)

    try:
        return import_past_trade_seed(auth_user["id"])
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


# -------------------- APIs --------------------
@app.get("/api/scoreboard")
def api_scoreboard(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    try:
        return scoreboard_all_time()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@app.get("/api/trades")
def api_get_trades(request: Request, view: str = "all"):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user
    try:
        maybe_import_founder_past_trades(auth_user)
        rows = trades_select(view=view, user_id=auth_user["id"])
        return {"ok": True, "trades": rows}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@app.post("/api/trades")
async def api_create_trade(request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user

    payload = await request.json()

    user_id = auth_user["id"]
    trade_id = str(uuid.uuid4())

    scan_date_ct = payload.get("scan_date_ct") or (STATE.get("meta", {}).get("date") if STATE else None) or ct_date()
    entry_time_ct = payload.get("entry_time_ct") or now_ct_str()
    exit_time_ct = payload.get("exit_time_ct")
    exit_price = payload.get("exit_price")

    entry_price = payload.get("entry_price")
    shares = payload.get("shares")

    row = {
        "id": trade_id,
        "user_id": user_id,
        "created_at_utc": datetime.utcnow().isoformat(),
        "scan_id": payload.get("scan_id"),
        "scan_date_ct": scan_date_ct,
        "ticker": (payload.get("ticker") or "").upper().strip(),
        "bucket": payload.get("bucket"),
        "subtype": payload.get("subtype"),
        "confidence": payload.get("confidence"),
        "plan": payload.get("plan"),
        "trigger": payload.get("trigger"),
        "stop": payload.get("stop"),
        "scan_close": payload.get("scan_close"),
        "move_pct": payload.get("move_pct"),
        "dollar_vol": payload.get("dollar_vol"),
        "range_pct": payload.get("range_pct"),
        "hold_pct": payload.get("hold_pct"),
        "rel_vol": payload.get("rel_vol"),
        "si_pct_ff": payload.get("si_pct_ff"),
        "ctb": payload.get("ctb"),
        "avail": payload.get("avail"),
        "entry_price": float(entry_price) if entry_price not in (None, "") else None,
        "entry_time_ct": entry_time_ct,
        "exit_price": float(exit_price) if exit_price not in (None, "") else None,
        "exit_time_ct": exit_time_ct,
        "shares": float(shares) if shares not in (None, "") else None,
        "review_flags": json.dumps(payload.get("review_flags") or []),
    }

    try:
        trade_insert(row)
        return {"ok": True, "id": trade_id, "user_id": user_id}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@app.patch("/api/trades/{trade_id}")
async def api_close_trade(trade_id: str, request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user

    payload = await request.json()
    exit_price = payload.get("exit_price")
    if exit_price is None:
        return JSONResponse({"ok": False, "error": "exit_price is required"}, status_code=400)

    exit_time_ct = payload.get("exit_time_ct") or now_ct_str()

    try:
        trade_close(trade_id, float(exit_price), exit_time_ct, user_id=auth_user["id"])
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@app.post("/api/trades/{trade_id}/review")
async def api_review_trade(trade_id: str, request: Request):
    auth_user = require_user(request)
    if isinstance(auth_user, JSONResponse):
        return auth_user

    payload = await request.json()
    note = (payload.get("note") or "").strip()

    # find trade
    try:
        rows = trades_select(view="all", user_id=auth_user["id"])
        t = next((x for x in rows if x.get("id") == trade_id), None)
        if not t:
            return JSONResponse({"ok": False, "error": "Trade not found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)

    issues: list[str] = []
    tips: list[str] = []
    review_flags: list[dict] = []

    ep = t.get("entry_price")
    xp = t.get("exit_price")
    sh = t.get("shares")
    subtype = (t.get("subtype") or "").lower()
    plan = (t.get("plan") or "").strip()

    if ep is None or sh is None:
        issues.append("Missing entry price or shares (can’t compute risk/P&L cleanly).")
        review_flags.append({"icon": "⚠️", "label": "Missing entry/shares"})

    if xp is None:
        tips.append("Trade is still open. Add an exit to get a full post-trade review.")
        review_flags.append({"icon": "⏳", "label": "Still open"})

    if not subtype:
        tips.append("Add a setup label (ex: VWAP pullback, Break+PB). This helps pattern recognition.")
        review_flags.append({"icon": "🏷️", "label": "No setup label"})

    if not plan and not note:
        tips.append("Add 1–2 sentences: why entry, where stop should be, and what target/scale plan was.")

    if "vwap" in subtype or ("vwap" in note.lower() if note else False):
        tips.append(
            "VWAP pullbacks: best entries are reclaim/hold at VWAP with volume returning. Avoid chasing extended candles above VWAP."
        )
    else:
        tips.append(
            "Break→pullback: focus on the retest holding a key level (break line / premarket high) with volume staying elevated."
        )

    review_lines: list[str] = []
    review_lines.append(f"{t.get('ticker','')} • {t.get('subtype','') or 'setup'}")

    if ep is not None and xp is not None and sh is not None:
        try:
            pnl = (float(xp) - float(ep)) * float(sh)
            pnl_pct = (float(xp) - float(ep)) / float(ep) * 100 if float(ep) != 0 else None
            if pnl_pct is not None:
                review_lines.append(f"P/L: {pnl:+.2f} ({pnl_pct:+.1f}%)")
            else:
                review_lines.append(f"P/L: {pnl:+.2f}")

            if pnl < 0:
                tips.append("Loss review: was the stop respected? If not, reduce size or set a hard stop alert next time.")
            else:
                tips.append("Win review: did you scale out into strength and keep a runner? If yes, note the execution rules.")
        except Exception:
            pass

    if issues:
        review_lines.append("\nCorrections:")
        for it in issues:
            review_lines.append(f"• {it}")

    if tips:
        review_lines.append("\nCoaching:")
        for it in tips[:8]:
            review_lines.append(f"• {it}")

    if note:
        review_lines.append("\nYour notes:")
        review_lines.append(note)

    review_text = "\n".join(review_lines).strip()

    try:
        trade_save_review(trade_id, review_text, user_id=auth_user["id"])
    except Exception:
        pass

    try:
        trade_save_flags(trade_id, review_flags, user_id=auth_user["id"])
    except Exception:
        pass

    return {"ok": True, "review": review_text}
