# main.py
import os
import uuid
import queue
import threading
import traceback
from pathlib import Path
from datetime import datetime, timezone, timedelta, date

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse

import scanner

# --- Postgres (raw SQL) ---
import psycopg
from psycopg.rows import dict_row

# Timezone (Central) for “yesterday” logic + timestamps
try:
    from zoneinfo import ZoneInfo
    CT_TZ = ZoneInfo("America/Chicago")
except Exception:
    CT_TZ = timezone(timedelta(hours=-6))

app = FastAPI()

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
OUT_DIR = BASE_DIR / "outputs"
OUT_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/outputs", StaticFiles(directory=str(OUT_DIR)), name="outputs")

LOG_PATH = OUT_DIR / "live_log.txt"

DATABASE_URL = os.getenv("DATABASE_URL")  # Render Postgres injects this

# ---------------------------
# Scan session state
# ---------------------------
STATE_LOCK = threading.Lock()
SCANS: dict[str, dict] = {}
# SCANS[scan_id] = {
#   "running": bool,
#   "q": queue.Queue[str],
#   "last_report": str|None,
#   "started_utc": str,
# }

def _append_log_line(line: str):
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def clear_log_file():
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        f.write("")


def publish(scan_id: str, event: str, data: dict):
    """
    Put SSE event into this scan's queue.
    """
    payload = f"event: {event}\ndata: {scanner.json_dumps(data)}\n\n"
    with STATE_LOCK:
        s = SCANS.get(scan_id)
        if not s:
            return
        s["q"].put(payload)


def log_line(scan_id: str, msg: str):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    _append_log_line(line)
    publish(scan_id, "log", {"line": line})


# ---------------------------
# DB helpers + schema init
# ---------------------------
def db_required():
    if not DATABASE_URL:
        raise HTTPException(
            status_code=500,
            detail="DATABASE_URL not set. Create a Render Postgres DB and attach it to this service."
        )

def db_conn():
    db_required()
    # psycopg3 supports DATABASE_URL directly
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def init_db():
    if not DATABASE_URL:
        # allow app to boot, but notebook endpoints will error clearly
        return

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS trades (
              id                TEXT PRIMARY KEY,
              user_id           TEXT NOT NULL,
              scan_id           TEXT,
              scan_date_ct      DATE,

              ticker            TEXT NOT NULL,
              bucket            TEXT,
              subtype           TEXT,
              confidence        INTEGER,
              plan              TEXT,

              -- scanner snapshot (read-only history)
              trigger           DOUBLE PRECISION,
              stop              DOUBLE PRECISION,
              scan_close        DOUBLE PRECISION,
              move_pct          DOUBLE PRECISION,
              dollar_vol        DOUBLE PRECISION,
              range_pct         DOUBLE PRECISION,
              hold_pct          DOUBLE PRECISION,
              rel_vol           DOUBLE PRECISION,

              -- execution
              entry_price       DOUBLE PRECISION NOT NULL,
              shares            DOUBLE PRECISION NOT NULL,
              entry_time_ct     TIMESTAMPTZ NOT NULL,

              exit_price        DOUBLE PRECISION,
              exit_time_ct      TIMESTAMPTZ,

              notes             TEXT,

              review_tomorrow   BOOLEAN NOT NULL DEFAULT FALSE,
              grade_1_10        INTEGER,

              -- flags
              auto_flags        TEXT NOT NULL DEFAULT '[]',
              review_flags      TEXT NOT NULL DEFAULT '[]',
              lesson            TEXT,

              created_at_utc    TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """)
        conn.commit()

@app.on_event("startup")
def _startup():
    init_db()


# ---------------------------
# Flags + P/L logic
# ---------------------------
RIGHT_ICON = "✅"
WARN_ICON  = "⚠️"
BAD_ICON   = "🚩"

def compute_auto_flags(t: dict) -> list[dict]:
    """
    Returns list of {key,label,level,icon}
    level: good|warn|bad
    """
    out = []

    entry = t.get("entry_price")
    exitp = t.get("exit_price")
    trig = t.get("trigger")
    stop = t.get("stop")
    entry_time = t.get("entry_time_ct")
    exit_time = t.get("exit_time_ct")

    def add(key, label, level):
        icon = RIGHT_ICON if level == "good" else (WARN_ICON if level == "warn" else BAD_ICON)
        out.append({"key": key, "label": label, "level": level, "icon": icon})

    if entry is not None and trig is not None and trig > 0:
        if entry > trig * 1.02:
            add("chased_entry", "Chased entry (>2% above trigger)", "bad")
        elif abs(entry - trig) / trig <= 0.005:
            add("entered_at_trigger", "Entered near trigger", "good")

    # Only judge stop behavior when trade is closed
    if exitp is not None:
        if stop is not None and stop > 0:
            if exitp < stop * 0.995:
                add("stop_ignored", "Exit below stop (stop ignored)", "bad")
            elif abs(exitp - stop) / stop <= 0.01:
                add("stop_respected", "Stop respected (exit near stop)", "good")

        # “Held too long” (simple rule): > 4 hours
        if entry_time is not None and exit_time is not None:
            try:
                mins = (exit_time - entry_time).total_seconds() / 60.0
                if mins > 240:
                    add("held_too_long", "Held too long (>4h)", "warn")
            except Exception:
                pass

        # Profit-taking (simple): >= +3% from entry
        if entry is not None and entry > 0:
            if exitp >= entry * 1.03:
                add("took_profit", "Took profit (>= +3%)", "good")
            elif exitp <= entry * 0.985:
                add("cut_loss", "Cut a loss (<= -1.5%)", "warn")

    return out

def pnl_calc(entry_price, exit_price, shares):
    if entry_price is None or exit_price is None or shares is None:
        return None, None
    try:
        pnl_d = (float(exit_price) - float(entry_price)) * float(shares)
        pnl_p = (float(exit_price) - float(entry_price)) / float(entry_price) if float(entry_price) != 0 else None
        return pnl_d, pnl_p
    except Exception:
        return None, None


# ---------------------------
# Scanner thread
# ---------------------------
def do_scan(scan_id: str):
    try:
        log_line(scan_id, "Scanner thread started.")

        html_path = scanner.run_scan(
            log_fn=lambda m: log_line(scan_id, m),
            row_fn=lambda row: publish(scan_id, "row", row),
        )

        with STATE_LOCK:
            if scan_id in SCANS:
                SCANS[scan_id]["last_report"] = html_path

        publish(
            scan_id,
            "done",
            {"ok": True, "report_url": f"/{html_path.lstrip('/')}" if html_path else None},
        )

        log_line(scan_id, "Scan finished.")

    except Exception:
        log_line(scan_id, "SCAN CRASHED — traceback below:")
        tb = traceback.format_exc()
        for ln in tb.splitlines():
            log_line(scan_id, ln)
        publish(scan_id, "done", {"ok": False, "error": "Scan crashed. Check log."})

    finally:
        with STATE_LOCK:
            if scan_id in SCANS:
                SCANS[scan_id]["running"] = False


# ---------------------------
# Routes: app + scan
# ---------------------------
@app.get("/")
def home():
    return RedirectResponse(url="/static/index.html")


@app.post("/run_scan")
def run_scan():
    scan_id = str(uuid.uuid4())
    started = datetime.now(timezone.utc).isoformat()

    with STATE_LOCK:
        SCANS[scan_id] = {
            "running": True,
            "q": queue.Queue(),
            "last_report": None,
            "started_utc": started,
        }

    publish(scan_id, "meta", {"scan_id": scan_id, "started_utc": started})

    t = threading.Thread(target=do_scan, args=(scan_id,), daemon=True)
    t.start()

    return JSONResponse({"ok": True, "scan_id": scan_id})


@app.get("/stream/{scan_id}")
def stream(scan_id: str):
    with STATE_LOCK:
        s = SCANS.get(scan_id)
        if not s:
            return JSONResponse({"ok": False, "error": "Unknown scan_id"}, status_code=404)
        q = s["q"]

    def gen():
        yield "event: ping\ndata: {}\n\n"
        while True:
            try:
                msg = q.get(timeout=25)
                yield msg
            except queue.Empty:
                yield "event: ping\ndata: {}\n\n"
                with STATE_LOCK:
                    alive = SCANS.get(scan_id)
                    if not alive:
                        break
                    if not alive["running"] and alive["q"].empty():
                        break

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)


@app.get("/scan_log")
def scan_log():
    if not LOG_PATH.exists():
        return ""
    return FileResponse(LOG_PATH, media_type="text/plain")


@app.post("/clear_log")
def clear_log():
    clear_log_file()
    return {"ok": True}


# ---------------------------
# Routes: Notebook API (Postgres)
# ---------------------------
@app.get("/api/trades")
def api_get_trades(
    view: str = Query("all", pattern="^(all|yesterday)$"),
    user_id: str = Query("demo")
):
    db_required()
    now_ct = datetime.now(tz=CT_TZ)
    y_ct = (now_ct.date() - timedelta(days=1))
    y_start = datetime(y_ct.year, y_ct.month, y_ct.day, 0, 0, 0, tzinfo=CT_TZ)
    y_end = y_start + timedelta(days=1)

    where = "WHERE user_id = %s"
    params = [user_id]

    if view == "yesterday":
        # Yesterday review: only CLOSED trades, whose exit_time is yesterday CT
        where += " AND exit_time_ct IS NOT NULL AND exit_time_ct >= %s AND exit_time_ct < %s"
        params.extend([y_start, y_end])

    q = f"""
    SELECT *
    FROM trades
    {where}
    ORDER BY created_at_utc DESC
    """

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q, params)
            rows = cur.fetchall()

    # Attach computed P/L and convenience fields
    out = []
    for r in rows:
        pnl_d, pnl_p = pnl_calc(r.get("entry_price"), r.get("exit_price"), r.get("shares"))
        r["pnl_dollars"] = pnl_d
        r["pnl_pct"] = pnl_p
        out.append(r)

    return {"ok": True, "trades": out}


@app.post("/api/trades")
def api_create_trade(payload: dict):
    """
    Required: ticker, entry_price, shares
    We also accept scanner snapshot fields from the scan row.
    """
    db_required()

    user_id = payload.get("user_id") or "demo"
    trade_id = str(uuid.uuid4())

    ticker = (payload.get("ticker") or "").upper().strip()
    if not ticker:
        raise HTTPException(status_code=400, detail="ticker required")

    entry_price = payload.get("entry_price")
    shares = payload.get("shares")
    if entry_price is None or shares is None:
        raise HTTPException(status_code=400, detail="entry_price and shares required")

    try:
        entry_price = float(entry_price)
        shares = float(shares)
    except Exception:
        raise HTTPException(status_code=400, detail="entry_price/shares must be numbers")

    if entry_price <= 0 or shares <= 0:
        raise HTTPException(status_code=400, detail="entry_price and shares must be > 0")

    # Timestamps (CT)
    entry_time_ct = datetime.now(tz=CT_TZ)

    scan_date_ct = payload.get("scan_date_ct")
    if scan_date_ct:
        try:
            scan_date_ct = datetime.fromisoformat(scan_date_ct).date()
        except Exception:
            scan_date_ct = None

    notes = payload.get("notes") or ""

    # Insert
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO trades (
              id, user_id, scan_id, scan_date_ct,
              ticker, bucket, subtype, confidence, plan,
              trigger, stop, scan_close, move_pct, dollar_vol, range_pct, hold_pct, rel_vol,
              entry_price, shares, entry_time_ct,
              notes,
              review_tomorrow
            ) VALUES (
              %s, %s, %s, %s,
              %s, %s, %s, %s, %s,
              %s, %s, %s, %s, %s, %s, %s, %s,
              %s, %s, %s,
              %s,
              %s
            )
            """, (
                trade_id, user_id, payload.get("scan_id"), scan_date_ct,
                ticker, payload.get("bucket"), payload.get("subtype"), payload.get("confidence"), payload.get("plan"),
                payload.get("trigger"), payload.get("stop"), payload.get("scan_close"),
                payload.get("move_pct"), payload.get("dollar_vol"),
                payload.get("range_pct"), payload.get("hold_pct"), payload.get("rel_vol"),
                entry_price, shares, entry_time_ct,
                notes,
                bool(payload.get("review_tomorrow") or False)
            ))
        conn.commit()

    # Return trade
    return api_get_trade(trade_id, user_id=user_id)


@app.get("/api/trades/{trade_id}")
def api_get_trade(trade_id: str, user_id: str = Query("demo")):
    db_required()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM trades WHERE id=%s AND user_id=%s", (trade_id, user_id))
            r = cur.fetchone()
            if not r:
                raise HTTPException(status_code=404, detail="trade not found")

    pnl_d, pnl_p = pnl_calc(r.get("entry_price"), r.get("exit_price"), r.get("shares"))
    r["pnl_dollars"] = pnl_d
    r["pnl_pct"] = pnl_p
    return {"ok": True, "trade": r}


@app.patch("/api/trades/{trade_id}")
def api_update_trade(trade_id: str, payload: dict, user_id: str = Query("demo")):
    db_required()

    # Fetch existing
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM trades WHERE id=%s AND user_id=%s", (trade_id, user_id))
            existing = cur.fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="trade not found")

        # Build updates
        allowed = {
            "entry_price", "shares", "notes",
            "exit_price", "exit_time_ct",
            "review_tomorrow", "grade_1_10",
            "review_flags", "lesson"
        }
        sets = []
        params = []

        for k, v in payload.items():
            if k not in allowed:
                continue

            if k in ("entry_price", "shares", "exit_price") and v is not None:
                try:
                    v = float(v)
                except Exception:
                    raise HTTPException(status_code=400, detail=f"{k} must be number")

            if k == "grade_1_10" and v is not None:
                try:
                    v = int(v)
                except Exception:
                    raise HTTPException(status_code=400, detail="grade_1_10 must be integer")
                if v < 1 or v > 10:
                    raise HTTPException(status_code=400, detail="grade_1_10 must be 1..10")

            if k == "review_tomorrow":
                v = bool(v)

            # exit_time_ct: if not provided but exit_price is being set, we’ll set it automatically
            if k == "exit_time_ct" and v:
                try:
                    v = datetime.fromisoformat(v)
                except Exception:
                    v = None

            if k == "review_flags":
                # store as JSON string (list)
                if isinstance(v, list):
                    v = scanner.json_dumps(v)
                elif isinstance(v, str):
                    # trust the client string (must be JSON list)
                    v = v
                else:
                    v = "[]"

            sets.append(f"{k}=%s")
            params.append(v)

        # If user sets exit_price but doesn't set exit_time_ct, set it now
        if "exit_price" in payload and payload.get("exit_price") is not None and "exit_time_ct" not in payload:
            sets.append("exit_time_ct=%s")
            params.append(datetime.now(tz=CT_TZ))

        if sets:
            params.extend([trade_id, user_id])
            with conn.cursor() as cur:
                cur.execute(f"UPDATE trades SET {', '.join(sets)} WHERE id=%s AND user_id=%s", params)

        # Recompute flags if trade is now closed (exit_price exists)
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM trades WHERE id=%s AND user_id=%s", (trade_id, user_id))
            updated = cur.fetchone()

        auto = compute_auto_flags(updated)
        auto_json = scanner.json_dumps(auto)

        # If trade is closed and review_flags empty, default to auto flags
        review_flags = updated.get("review_flags") or "[]"
        if updated.get("exit_price") is not None:
            if review_flags == "[]" or review_flags.strip() == "":
                review_flags = auto_json

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE trades SET auto_flags=%s, review_flags=%s WHERE id=%s AND user_id=%s",
                (auto_json, review_flags, trade_id, user_id)
            )

        conn.commit()

    return api_get_trade(trade_id, user_id=user_id)


@app.delete("/api/trades/{trade_id}")
def api_delete_trade(trade_id: str, user_id: str = Query("demo")):
    db_required()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM trades WHERE id=%s AND user_id=%s", (trade_id, user_id))
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="trade not found")
        conn.commit()
    return {"ok": True}
