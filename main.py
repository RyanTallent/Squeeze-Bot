import traceback
import os
import json
import time
import uuid
import threading
import sqlite3
from pathlib import Path
from collections import deque
from datetime import datetime, timedelta

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

# Optional Postgres (only used if DATABASE_URL is set)
try:
    import psycopg
    from psycopg.rows import dict_row
except Exception:
    psycopg = None
    dict_row = None

import scanner  # your scanner.py


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
OUT_DIR = BASE_DIR / "outputs"
OUT_DIR.mkdir(exist_ok=True)

SQLITE_PATH = OUT_DIR / "trades.sqlite3"


def using_postgres() -> bool:
    return bool(DATABASE_URL) and psycopg is not None


def pg_conn():
    # Only call when using_postgres() is True
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def sqlite_conn():
    # Built-in DB, always available
    conn = sqlite3.connect(str(SQLITE_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    # Same schema for both
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
        conn = sqlite_conn()
        try:
            cur = conn.cursor()
            cur.execute(schema_sql)
            conn.commit()
        finally:
            conn.close()


def trades_select(view: str, user_id: str | None) -> list[dict]:
    view = (view or "all").lower().strip()
    if view not in ("all", "yesterday"):
        view = "all"

    where = []
    params = []

    if user_id:
        where.append("user_id = %s")
        params.append(user_id)

    if view == "yesterday":
        where.append("scan_date_ct = %s")
        params.append(yesterday_ct_date())

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    sql = f"""
    SELECT
      id, user_id, scan_id, scan_date_ct,
      ticker, bucket, subtype, confidence, plan,
      trigger, stop, scan_close, move_pct, dollar_vol, range_pct, hold_pct, rel_vol, si_pct_ff, ctb, avail,
      entry_price, entry_time_ct, exit_price, exit_time_ct, shares,
      review_flags
    FROM trades
    {where_sql}
    ORDER BY created_at_utc DESC
    LIMIT 500;
    """

    if using_postgres():
        # Postgres uses %s placeholders already
        with pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        rows = [dict(r) for r in rows]
    else:
        # SQLite uses ? placeholders
        sql_sqlite = sql.replace("%s", "?")
        conn = sqlite_conn()
        try:
            cur = conn.cursor()
            cur.execute(sql_sqlite, params)
            rows = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    # normalize review_flags + add pnl fields
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
                      %(id)s, %(user_id)s, NOW(),
                      %(scan_id)s, %(scan_date_ct)s,
                      %(ticker)s, %(bucket)s, %(subtype)s, %(confidence)s, %(plan)s,
                      %(trigger)s, %(stop)s, %(scan_close)s, %(move_pct)s, %(dollar_vol)s, %(range_pct)s, %(hold_pct)s, %(rel_vol)s, %(si_pct_ff)s, %(ctb)s, %(avail)s,
                      %(entry_price)s, %(entry_time_ct)s, %(exit_price)s, %(exit_time_ct)s, %(shares)s, %(review_flags)s
                    );
                    """,
                    row,
                )
                conn.commit()
    else:
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
                    row["id"], row["user_id"], row["created_at_utc"],
                    row.get("scan_id"), row.get("scan_date_ct"),
                    row.get("ticker"), row.get("bucket"), row.get("subtype"), row.get("confidence"), row.get("plan"),
                    row.get("trigger"), row.get("stop"), row.get("scan_close"), row.get("move_pct"), row.get("dollar_vol"),
                    row.get("range_pct"), row.get("hold_pct"), row.get("rel_vol"), row.get("si_pct_ff"), row.get("ctb"), row.get("avail"),
                    row.get("entry_price"), row.get("entry_time_ct"), row.get("exit_price"), row.get("exit_time_ct"),
                    row.get("shares"), row.get("review_flags"),
                ),
            )
            conn.commit()
        finally:
            conn.close()


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
    else:
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
app.mount("/outputs", StaticFiles(directory=str(OUT_DIR)), name="outputs")


@app.on_event("startup")
def _startup():
    db_init()


# -------------------- scan state (in-memory) --------------------
STATE_LOCK = threading.Lock()
STATE = {
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


def _state_snapshot():
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


# -------------------- routes --------------------
@app.get("/")
def root():
    return RedirectResponse(url="/static/index.html")


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
    import os
    return {
        "POLYGON_API_KEY_set": bool(os.getenv("POLYGON_API_KEY")),
        "ORTEX_API_KEY_set": bool(os.getenv("ORTEX_API_KEY")),
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
def run_scan(mode: str = "auto", ortex: str = "off"):
    try:
        mode = (mode or "auto").strip().lower()
        ortex = (ortex or "off").strip().lower()

        if mode not in ("auto", "day", "night"):
            mode = "auto"
        if ortex not in ("on", "off"):
            ortex = "off"

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

        # start worker AFTER lock released
        th = threading.Thread(
            target=_scan_worker,
            args=(scan_id, eff_mode, ortex_for_worker),
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
        return JSONResponse(
            {"ok": False, "error": f"{type(e).__name__}: {str(e)}", "trace": tb[-1500:]},
            status_code=500,
        )


def _scan_worker(scan_id: str, mode: str, ortex: str):
    try:
        push_log("Starting scan… (manual)")
        push_log(f"Worker params → mode={mode} | ortex={ortex}")

        html_path = None

        try:
            html_path = scanner.run_scan(
                log_fn=push_log,
                row_fn=push_row,
                mode=mode,
                ortex=ortex,
            )
        except Exception as scan_err:
            push_log(f"[SCANNER ERROR] {type(scan_err).__name__}: {str(scan_err)}")
            mark_done(False, None)
            return

        if html_path:
            push_log(f"Saved HTML: {html_path}")
        else:
            push_log("Scan completed. No candidates passed filters.")

        mark_done(True, html_path)

    except Exception as e:
        push_log(f"[FATAL WORKER ERROR] {type(e).__name__}: {str(e)}")
        mark_done(False, None)



@app.get("/stream/{scan_id}")
def stream(scan_id: str):
    def event_gen():
        last_log_seq = 0
        last_row_seq = 0
        last_meta_seq = 0
        last_done_seq = 0

        snap = _state_snapshot()
        if snap["scan_id"] != scan_id:
            yield _sse("done", {"ok": False, "error": "Unknown scan_id"})
            return

        # initial meta
        yield _sse("meta", snap["meta"])

        while True:
            snap = _state_snapshot()

            if snap["log_seq"] != last_log_seq:
                with STATE_LOCK:
                    logs = list(STATE["logs"])
                    seq = STATE["log_seq"]
                for line in logs[-60:]:
                    yield _sse("log", {"line": line})
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

            # heartbeat keeps proxies happy
            yield ": ping\n\n"
            time.sleep(0.35)

    return _sse_response(event_gen)


def _sse(event_name: str, data_obj: dict):
    return f"event: {event_name}\ndata: {json.dumps(data_obj, ensure_ascii=False)}\n\n"


def _sse_response(gen_fn):
    from starlette.responses import StreamingResponse
    return StreamingResponse(gen_fn(), media_type="text/event-stream")


# -------------------- TRADES API (NOTEBOOK) --------------------
@app.get("/api/trades")
def api_get_trades(view: str = "all", user_id: str | None = None):
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

    scan_date_ct = payload.get("scan_date_ct") or (STATE.get("meta", {}).get("date") if STATE else None) or ct_date()
    entry_time_ct = payload.get("entry_time_ct") or now_ct_str()

    entry_price = payload.get("entry_price")
    shares = payload.get("shares")

    row = {
        "id": trade_id,
        "user_id": user_id,
        "created_at_utc": datetime.utcnow().isoformat(),

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
        trade_insert(row)
        return {"ok": True, "id": trade_id, "user_id": user_id}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@app.patch("/api/trades/{trade_id}")
async def api_close_trade(trade_id: str, request: Request, user_id: str | None = None):
    payload = await request.json()
    exit_price = payload.get("exit_price")
    if exit_price is None:
        return JSONResponse({"ok": False, "error": "exit_price is required"}, status_code=400)

    exit_time_ct = payload.get("exit_time_ct") or now_ct_str()

    try:
        trade_close(trade_id, float(exit_price), exit_time_ct, user_id=user_id)
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)
