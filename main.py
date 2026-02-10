# main.py
import os
import json
import time
import uuid
import threading
from pathlib import Path
from collections import deque
from datetime import datetime, timedelta

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

import psycopg
from psycopg.rows import dict_row

import scanner  # your scanner.py

# -------------------- timezone helpers (CT) --------------------
try:
    from zoneinfo import ZoneInfo
    CT_TZ = ZoneInfo("America/Chicago")
except Exception:
    CT_TZ = None

def now_ct_str() -> str:
    dt = datetime.now(tz=CT_TZ) if CT_TZ else datetime.now()
    # Render linux supports %-I; Windows doesn't. We'll be safe.
    try:
        return dt.strftime("%-I:%M %p CT")
    except Exception:
        return dt.strftime("%I:%M %p CT").lstrip("0")

def ct_date(dt: datetime | None = None) -> str:
    dt = dt or (datetime.now(tz=CT_TZ) if CT_TZ else datetime.now())
    return dt.strftime("%Y-%m-%d")

def yesterday_ct_date() -> str:
    dt = (datetime.now(tz=CT_TZ) if CT_TZ else datetime.now()) - timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


# -------------------- DB --------------------
DATABASE_URL = os.getenv("DATABASE_URL")

def db_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set in Render environment variables.")
    # psycopg v3 wants postgresql://... (Render provides that)
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def db_init():
    # Simple durable storage for your notebook
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
            """)
            conn.commit()

def compute_flags(t: dict) -> list[dict]:
    # Optional: you can expand later. For now keep empty unless you stored flags.
    try:
        raw = t.get("review_flags")
        if not raw:
            return []
        if isinstance(raw, str):
            return json.loads(raw)
        return raw
    except Exception:
        return []

def trades_select(view: str, user_id: str) -> list[dict]:
    view = (view or "all").lower().strip()
    if view not in ("all", "yesterday"):
        view = "all"

    where = "WHERE user_id = %s"
    params = [user_id]

    if view == "yesterday":
        where += " AND scan_date_ct = %s"
        params.append(yesterday_ct_date())

    sql = f"""
    SELECT
      id, user_id, scan_id, scan_date_ct,
      ticker, bucket, subtype, confidence, plan,
      trigger, stop, scan_close, move_pct, dollar_vol, range_pct, hold_pct, rel_vol, si_pct_ff, ctb, avail,
      entry_price, entry_time_ct, exit_price, exit_time_ct, shares,
      review_flags,
      CASE
        WHEN exit_price IS NULL OR entry_price IS NULL OR shares IS NULL THEN NULL
        ELSE (exit_price - entry_price) * shares
      END AS pnl_dollars,
      CASE
        WHEN exit_price IS NULL OR entry_price IS NULL OR entry_price = 0 THEN NULL
        ELSE (exit_price - entry_price) / entry_price
      END AS pnl_pct
    FROM trades
    {where}
    ORDER BY created_at_utc DESC
    LIMIT 500;
    """

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    # normalize JSON fields
    for r in rows:
        r["review_flags"] = json.dumps(compute_flags(r))
    return rows


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
app.mount("/static", StaticFiles(directory="static"), name="static")

OUT_DIR = Path("outputs")
OUT_DIR.mkdir(exist_ok=True)
app.mount("/outputs", StaticFiles(directory=str(OUT_DIR)), name="outputs")


@app.on_event("startup")
def _startup():
    # create DB table on boot (so notebook persists across deploys)
    db_init()


# -------------------- simple in-memory scan state --------------------
STATE_LOCK = threading.Lock()
STATE = {
    "running": False,
    "scan_id": None,
    "started_at_ct": None,
    "logs": deque(maxlen=2500),
    "rows": deque(maxlen=200),
    "meta": {
        "mode": "auto",
        "ortex_requested": "auto",
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

def _state_snapshot():
    with STATE_LOCK:
        return json.loads(json.dumps({
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
        }))

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


# -------------------- routes --------------------
@app.get("/")
def root():
    return RedirectResponse(url="/static/index.html")

@app.get("/health")
def health():
    snap = _state_snapshot()
    return {"ok": True, "running": snap["running"], "scan_id": snap["scan_id"], "meta": snap["meta"]}

@app.post("/clear_log")
def clear_log():
    with STATE_LOCK:
        STATE["logs"].clear()
        STATE["rows"].clear()
        STATE["log_seq"] += 1
        STATE["row_seq"] += 1
    return {"ok": True}

@app.post("/run_scan")
def run_scan(request: Request, mode: str = "auto", ortex: str = "auto"):
    mode = (mode or "auto").strip().lower()
    ortex = (ortex or "auto").strip().lower()

    if mode not in ("auto", "day", "night"):
        mode = "auto"
    if ortex not in ("auto", "on", "off"):
        ortex = "auto"

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

        dt = scanner.now_ct()
        window_name, w_start, w_end = scanner.current_market_window(dt)
        date_str = scanner.ct_date_str(w_start)

        if mode == "auto":
            eff_mode = "day" if window_name in ("PREMARKET", "REGULAR", "AFTERHOURS") else "night"
        else:
            eff_mode = mode

        # use scanner resolve_ortex_on if present
        try:
            ortex_on, ortex_label = scanner.resolve_ortex_on(eff_mode, ortex, dt)
        except Exception:
            ortex_on = (ortex == "on")
            ortex_label = "ON" if ortex_on else "OFF"

        STATE["started_at_ct"] = now_ct_str()
        STATE["meta"] = {
            "mode": eff_mode,
            "ortex_requested": ortex,
            "ortex_effective": ortex_label,
            "window": window_name,
            "date": date_str,
            "scanned_count": None,
        }

    th = threading.Thread(
        target=_scan_worker,
        args=(scan_id, STATE["meta"]["mode"], ortex),
        daemon=True
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

def _scan_worker(scan_id: str, mode: str, ortex: str):
    try:
        push_log("Starting scan… (manual)")
        push_log("Scanner thread started. Calling scanner.run_scan()...")

        html_path = scanner.run_scan(
            log_fn=push_log,
            row_fn=push_row,
            mode=mode,
            ortex=ortex,
        )

        if html_path:
            push_log(f"Saved HTML: {html_path}")
            mark_done(True, html_path)
        else:
            push_log("No candidates passed filters.")
            mark_done(True, None)

    except Exception as e:
        push_log(f"[FATAL] {type(e).__name__}: {str(e)[:200]}")
        mark_done(False, None)

@app.get("/stream/{scan_id}")
def stream(scan_id: str):
    def event_gen():
        last_log = 0
        last_row = 0
        last_meta = 0
        last_done = 0

        snap = _state_snapshot()
        if snap["scan_id"] != scan_id:
            yield _sse("done", {"ok": False, "error": "Unknown scan_id"})
            return

        while True:
            snap = _state_snapshot()

            if snap["log_seq"] != last_log:
                with STATE_LOCK:
                    logs = list(STATE["logs"])
                    seq = STATE["log_seq"]
                for line in logs[-50:]:
                    yield _sse("log", {"line": line})
                last_log = seq

            if snap["meta_seq"] != last_meta:
                yield _sse("meta", snap["meta"])
                last_meta = snap["meta_seq"]

            if snap["row_seq"] != last_row:
                with STATE_LOCK:
                    rows = list(STATE["rows"])
                    seq = STATE["row_seq"]
                for r in rows[-10:]:
                    yield _sse("row", r)
                last_row = seq

            if snap["done_seq"] != last_done and snap["done"]:
                payload = {"ok": snap["ok"], "html_path": snap["html_path"]}
                yield _sse("done", payload)
                return

            time.sleep(0.35)

    return _sse_response(event_gen)

def _sse(event_name: str, data_obj: dict):
    return f"event: {event_name}\ndata: {json.dumps(data_obj, ensure_ascii=False)}\n\n"

def _sse_response(gen_fn):
    from starlette.responses import StreamingResponse
    return StreamingResponse(gen_fn(), media_type="text/event-stream")


# -------------------- TRADES API (THIS FIXES NOTEBOOK) --------------------
@app.get("/api/trades")
def api_get_trades(view: str = "all", user_id: str = "demo"):
    try:
        rows = trades_select(view=view, user_id=user_id)
        return {"ok": True, "trades": rows}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)

@app.post("/api/trades")
async def api_create_trade(request: Request):
    payload = await request.json()

    user_id = payload.get("user_id") or "demo"
    trade_id = str(uuid.uuid4())

    # prefer scan meta date if provided
    scan_date_ct = payload.get("scan_date_ct") or (STATE.get("meta", {}).get("date") if STATE else None) or ct_date()

    entry_price = payload.get("entry_price")
    shares = payload.get("shares")

    # store times as CT strings for your UI
    entry_time_ct = payload.get("entry_time_ct") or now_ct_str()

    row = {
        "id": trade_id,
        "user_id": user_id,
        "scan_id": payload.get("scan_id"),
        "scan_date_ct": scan_date_ct,

        "ticker": payload.get("ticker"),
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

        "entry_price": float(entry_price) if entry_price is not None else None,
        "entry_time_ct": entry_time_ct,
        "exit_price": None,
        "exit_time_ct": None,
        "shares": float(shares) if shares is not None else None,

        "review_flags": json.dumps(payload.get("review_flags") or []),
    }

    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                INSERT INTO trades (
                  id, user_id, scan_id, scan_date_ct,
                  ticker, bucket, subtype, confidence, plan,
                  trigger, stop, scan_close, move_pct, dollar_vol, range_pct, hold_pct, rel_vol, si_pct_ff, ctb, avail,
                  entry_price, entry_time_ct, exit_price, exit_time_ct, shares, review_flags
                ) VALUES (
                  %(id)s, %(user_id)s, %(scan_id)s, %(scan_date_ct)s,
                  %(ticker)s, %(bucket)s, %(subtype)s, %(confidence)s, %(plan)s,
                  %(trigger)s, %(stop)s, %(scan_close)s, %(move_pct)s, %(dollar_vol)s, %(range_pct)s, %(hold_pct)s, %(rel_vol)s, %(si_pct_ff)s, %(ctb)s, %(avail)s,
                  %(entry_price)s, %(entry_time_ct)s, %(exit_price)s, %(exit_time_ct)s, %(shares)s, %(review_flags)s
                );
                """, row)
                conn.commit()
        return {"ok": True, "id": trade_id}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)

@app.patch("/api/trades/{trade_id}")
async def api_close_trade(trade_id: str, request: Request, user_id: str = "demo"):
    payload = await request.json()
    exit_price = payload.get("exit_price")
    if exit_price is None:
        return JSONResponse({"ok": False, "error": "exit_price is required"}, status_code=400)

    exit_time_ct = payload.get("exit_time_ct") or now_ct_str()

    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                UPDATE trades
                SET exit_price = %s, exit_time_ct = %s
                WHERE id = %s AND user_id = %s
                """, (float(exit_price), exit_time_ct, trade_id, user_id))
                conn.commit()
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)
