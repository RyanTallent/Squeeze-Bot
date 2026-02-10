# main.py
import os
import json
import time
import uuid
import threading
from pathlib import Path
from collections import deque
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

import scanner  # your scanner.py

app = FastAPI()

# If you ever open this from another domain, CORS helps.
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


# -------------------- simple in-memory state --------------------
STATE_LOCK = threading.Lock()

STATE = {
    "running": False,
    "scan_id": None,
    "started_at_ct": None,

    # streaming buffers
    "logs": deque(maxlen=2500),
    "rows": deque(maxlen=200),

    # meta (what UI wants)
    "meta": {
        "mode": "auto",
        "ortex_requested": "auto",
        "ortex_effective": "OFF",
        "window": "—",
        "date": None,
        "scanned_count": None,
    },

    # completion
    "done": False,
    "ok": True,
    "html_path": None,

    # monotonically increasing “version” for SSE
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
    # parse useful meta out of logs (so you don’t have to in UI)
    scanned = None
    window_name = None

    # examples:
    # Snapshot tickers received: 4212
    if "Snapshot tickers received:" in line:
        try:
            scanned = int(line.split("Snapshot tickers received:")[1].strip().split()[0])
        except Exception:
            scanned = None

    # Market window: PREMARKET (03:00–07:12 CT)
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
    # Your Render primary URL will now show the UI (no more {"detail":"Not Found"})
    return RedirectResponse(url="/static/index.html")


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

    # normalize to what scanner expects
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

        # compute meta BEFORE scan starts (no log parsing needed for these)
        dt = scanner.now_ct()
        window_name, w_start, w_end = scanner.current_market_window(dt)
        date_str = scanner.ct_date_str(w_start)

        # handle your scanner’s two conventions:
        # - if mode is auto, decide day/night based on time window (simple rule)
        if mode == "auto":
            # during market hours -> day, otherwise night
            if window_name in ("PREMARKET", "REGULAR", "AFTERHOURS"):
                eff_mode = "day"
            else:
                eff_mode = "night"
        else:
            eff_mode = mode

        # use scanner’s resolve_ortex_on if it exists (your latest scanner has it)
        try:
            ortex_on, ortex_label = scanner.resolve_ortex_on(eff_mode, ortex, dt)
        except Exception:
            ortex_on = (ortex == "on")
            ortex_label = "ON" if ortex_on else "OFF"

        started_at_ct = dt.strftime("%-I:%M %p CT") if "%" in "%-I" else dt.strftime("%I:%M %p CT").lstrip("0")

        STATE["started_at_ct"] = started_at_ct
        STATE["meta"] = {
            "mode": eff_mode,
            "ortex_requested": ortex,
            "ortex_effective": ortex_label,
            "window": window_name,
            "date": date_str,
            "scanned_count": None,  # filled once snapshot arrives
        }

    # start thread
    th = threading.Thread(
        target=_scan_worker,
        args=(scan_id, STATE["meta"]["mode"], ortex),
        daemon=True
    )
    th.start()

    # return meta immediately
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
    # SSE stream: sends log, row, meta, done events
    def event_gen():
        last_log = 0
        last_row = 0
        last_meta = 0
        last_done = 0

        # quick sanity: only allow current scan_id
        snap = _state_snapshot()
        if snap["scan_id"] != scan_id:
            yield _sse("done", {"ok": False, "error": "Unknown scan_id"})
            return

        while True:
            snap = _state_snapshot()

            # flush new logs
            if snap["log_seq"] != last_log:
                with STATE_LOCK:
                    logs = list(STATE["logs"])
                    seq = STATE["log_seq"]
                # send only new lines (best-effort: since deque can truncate)
                # easiest: just send the most recent chunk each time seq changes
                # but keep it light: last 50 lines
                for line in logs[-50:]:
                    yield _sse("log", {"line": line})
                last_log = seq

            # flush new meta
            if snap["meta_seq"] != last_meta:
                yield _sse("meta", snap["meta"])
                last_meta = snap["meta_seq"]

            # flush new rows
            if snap["row_seq"] != last_row:
                with STATE_LOCK:
                    rows = list(STATE["rows"])
                    seq = STATE["row_seq"]
                # send only last 10 rows each time (your scanner already only streams top 10 total)
                for r in rows[-10:]:
                    yield _sse("row", r)
                last_row = seq

            # done?
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


# ---- OPTIONAL: quick “health” ----
@app.get("/health")
def health():
    snap = _state_snapshot()
    return {"ok": True, "running": snap["running"], "scan_id": snap["scan_id"], "meta": snap["meta"]}
