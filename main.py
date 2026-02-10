# main.py
import os
import json
import uuid
import time
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Query, Body
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import StreamingResponse

import psycopg
from psycopg.rows import dict_row

import scanner  # your engine file MUST be named scanner.py


# -----------------------------
# App
# -----------------------------
app = FastAPI()

# Serve UI + outputs
app.mount("/static", StaticFiles(directory="static"), name="static")

OUT_DIR = Path("outputs")
OUT_DIR.mkdir(exist_ok=True)
app.mount("/outputs", StaticFiles(directory=str(OUT_DIR)), name="outputs")


@app.get("/")
def root():
    # Make base Render URL load your UI
    return RedirectResponse(url="/static/index.html")


# -----------------------------
# Database
# -----------------------------
DATABASE_URL = os.getenv("DATABASE_URL")  # Render Postgres sets this if you attach DB

def db_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set (attach Postgres on Render or set env var).")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def init_db():
    if not DATABASE_URL:
        # app can still run without notebook DB, but notebook endpoints will error
        return

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),

                scan_id TEXT,
                scan_date_ct TEXT,

                ticker TEXT NOT NULL,
                bucket TEXT,
                subtype TEXT,
                confidence INTEGER,
                plan TEXT,

                trigger DOUBLE PRECISION,
                stop DOUBLE PRECISION,
                scan_close DOUBLE PRECISION,
                move_pct DOUBLE PRECISION,
                dollar_vol DOUBLE PRECISION,
                range_pct DOUBLE PRECISION,
                hold_pct DOUBLE PRECISION,
                rel_vol DOUBLE PRECISION,

                si_pct_ff DOUBLE PRECISION,
                ctb DOUBLE PRECISION,
                avail DOUBLE PRECISION,

                entry_price DOUBLE PRECISION,
                shares DOUBLE PRECISION,
                entry_time_ct TEXT,

                exit_price DOUBLE PRECISION,
                exit_time_ct TEXT,

                review_flags TEXT
            );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_user_created ON trades(user_id, created_at_utc DESC);")
            conn.commit()

@app.on_event("startup")
def on_startup():
    init_db()


# -----------------------------
# Simple CT helpers (string-based)
# -----------------------------
def now_ct_iso() -> str:
    # scanner.CT_TZ exists (zoneinfo America/Chicago when available)
    dt = datetime.now(tz=scanner.CT_TZ)
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def ct_date_str_from_iso(ct_iso: str) -> str:
    # expects "YYYY-MM-DD HH:MM:SS"
    return (ct_iso or "")[:10]

def yesterday_ct_date() -> str:
    dt = datetime.now(tz=scanner.CT_TZ) - timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


# -----------------------------
# Scan state + SSE
# -----------------------------
LOCK = threading.Lock()

STATE = {
    "running": False,
    "scan_id": None,
    "subscribers": {},  # scan_id -> list of queues
}

def sse_send(scan_id: str, event: str, payload: Dict[str, Any]):
    with LOCK:
        subs = STATE["subscribers"].get(scan_id, [])
    # each subscriber queue is a list (simple, cheap)
    for q in subs:
        q.append((event, payload))

def log_line(scan_id: str, line: str):
    sse_send(scan_id, "log", {"line": line})

def emit_row(scan_id: str, row: Dict[str, Any]):
    sse_send(scan_id, "row", row)


@app.post("/clear_log")
def clear_log():
    # UI just clears its own box; this exists to match your fetch
    return {"ok": True}


@app.post("/run_scan")
def run_scan(
    mode: str = Query("auto"),     # auto|day|night
    ortex: str = Query("auto"),    # auto|on|off
):
    mode = (mode or "auto").strip().lower()
    if mode not in ("auto", "day", "night"):
        mode = "auto"

    ortex = (ortex or "auto").strip().lower()
    if ortex not in ("auto", "on", "off"):
        ortex = "auto"

    # Auto mode decision: during 7:30–16:00 CT => day, otherwise night
    if mode == "auto":
        dt = datetime.now(tz=scanner.CT_TZ)
        hhmm = dt.hour * 60 + dt.minute
        mode = "day" if (7 * 60 + 30) <= hhmm <= (16 * 60) else "night"

    with LOCK:
        if STATE["running"]:
            return JSONResponse({"ok": False, "error": "Scan already running"}, status_code=409)

        scan_id = uuid.uuid4().hex
        STATE["running"] = True
        STATE["scan_id"] = scan_id
        STATE["subscribers"][scan_id] = []

    def worker():
        try:
            log_line(scan_id, f"Starting scan… (manual)")
            html_path = scanner.run_scan(
                log_fn=lambda s: log_line(scan_id, s),
                row_fn=lambda r: emit_row(scan_id, r),
                mode=mode,
                ortex=ortex,
            )

            done = {"ok": True, "report_url": None, "mode": mode}
            if html_path:
                # scanner returns something like "outputs/scan_....html"
                done["report_url"] = "/" + html_path.lstrip("/")

            sse_send(scan_id, "done", done)

        except Exception as e:
            log_line(scan_id, f"[ERROR] {type(e).__name__}: {e}")
            sse_send(scan_id, "done", {"ok": False, "report_url": None, "mode": mode})

        finally:
            with LOCK:
                STATE["running"] = False

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "scan_id": scan_id, "mode": mode, "ortex": ortex}


@app.get("/stream/{scan_id}")
def stream(scan_id: str):
    q: List[Any] = []

    with LOCK:
        if scan_id not in STATE["subscribers"]:
            STATE["subscribers"][scan_id] = []
        STATE["subscribers"][scan_id].append(q)

    def gen():
        # keepalive + event drain loop
        last_ping = time.time()
        while True:
            if q:
                ev, payload = q.pop(0)
                yield f"event: {ev}\ndata: {scanner.json_dumps(payload)}\n\n"
                if ev == "done":
                    break
            else:
                # ping every ~1s
                if time.time() - last_ping >= 1.0:
                    yield "event: ping\ndata: {}\n\n"
                    last_ping = time.time()
                time.sleep(0.05)

    return StreamingResponse(gen(), media_type="text/event-stream")


# -----------------------------
# Notebook API
# -----------------------------
def compute_pnl(entry: Optional[float], exit_: Optional[float], shares: Optional[float]):
    if entry is None or exit_ is None or shares is None:
        return None, None
    try:
        pnl_d = (float(exit_) - float(entry)) * float(shares)
        pnl_p = (float(exit_) - float(entry)) / float(entry) if float(entry) != 0 else None
        return pnl_d, pnl_p
    except Exception:
        return None, None


@app.get("/api/trades")
def list_trades(
    user_id: str = Query("demo"),
    view: str = Query("all"),  # all|yesterday
):
    if not DATABASE_URL:
        return JSONResponse({"ok": False, "error": "DATABASE_URL not set"}, status_code=500)

    view = (view or "all").strip().lower()
    if view not in ("all", "yesterday"):
        view = "all"

    where = "WHERE user_id = %s"
    params: List[Any] = [user_id]

    if view == "yesterday":
        y = yesterday_ct_date()
        where += " AND entry_time_ct IS NOT NULL AND entry_time_ct LIKE %s"
        params.append(f"{y}%")

    sql = f"""
    SELECT *
    FROM trades
    {where}
    ORDER BY created_at_utc DESC
    LIMIT 500
    """

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    # add pnl fields
    out = []
    for r in rows:
        entry = r.get("entry_price")
        exit_ = r.get("exit_price")
        shares = r.get("shares")
        pnl_d, pnl_p = compute_pnl(entry, exit_, shares)
        r["pnl_dollars"] = pnl_d
        r["pnl_pct"] = pnl_p
        out.append(r)

    return {"ok": True, "trades": out}


@app.post("/api/trades")
def create_trade(payload: Dict[str, Any] = Body(...)):
    if not DATABASE_URL:
        return JSONResponse({"ok": False, "error": "DATABASE_URL not set"}, status_code=500)

    trade_id = uuid.uuid4().hex

    user_id = payload.get("user_id") or "demo"
    scan_id = payload.get("scan_id")
    scan_date_ct = payload.get("scan_date_ct")

    ticker = (payload.get("ticker") or "").strip().upper()
    if not ticker:
        return JSONResponse({"ok": False, "error": "ticker required"}, status_code=400)

    entry_price = payload.get("entry_price")
    shares = payload.get("shares")

    entry_time_ct = now_ct_iso()

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO trades (
                    id, user_id, scan_id, scan_date_ct,
                    ticker, bucket, subtype, confidence, plan,
                    trigger, stop, scan_close, move_pct, dollar_vol, range_pct, hold_pct, rel_vol,
                    si_pct_ff, ctb, avail,
                    entry_price, shares, entry_time_ct,
                    review_flags
                ) VALUES (
                    %s,%s,%s,%s,
                    %s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,
                    %s,%s,%s,
                    %s
                )
                """,
                (
                    trade_id, user_id, scan_id, scan_date_ct,
                    ticker, payload.get("bucket"), payload.get("subtype"), payload.get("confidence"), payload.get("plan"),
                    payload.get("trigger"), payload.get("stop"), payload.get("scan_close"), payload.get("move_pct"),
                    payload.get("dollar_vol"), payload.get("range_pct"), payload.get("hold_pct"), payload.get("rel_vol"),
                    payload.get("si_pct_ff"), payload.get("ctb"), payload.get("avail"),
                    float(entry_price) if entry_price is not None and str(entry_price) != "" else None,
                    float(shares) if shares is not None and str(shares) != "" else None,
                    entry_time_ct,
                    payload.get("review_flags") or "[]",
                )
            )
            conn.commit()

    return {"ok": True, "id": trade_id}


@app.patch("/api/trades/{trade_id}")
def close_trade(
    trade_id: str,
    user_id: str = Query("demo"),
    payload: Dict[str, Any] = Body(...),
):
    if not DATABASE_URL:
        return JSONResponse({"ok": False, "error": "DATABASE_URL not set"}, status_code=500)

    exit_price = payload.get("exit_price")
    if exit_price is None or str(exit_price).strip() == "":
        return JSONResponse({"ok": False, "error": "exit_price required"}, status_code=400)

    exit_time_ct = now_ct_iso()

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE trades
                SET exit_price = %s,
                    exit_time_ct = %s
                WHERE id = %s AND user_id = %s
                """,
                (float(exit_price), exit_time_ct, trade_id, user_id)
            )
            if cur.rowcount == 0:
                return JSONResponse({"ok": False, "error": "trade not found"}, status_code=404)
            conn.commit()

    return {"ok": True}
