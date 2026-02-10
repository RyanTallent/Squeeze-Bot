# main.py
import threading
import uuid
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import StreamingResponse

import scanner

app = FastAPI()

# Serve your UI
app.mount("/static", StaticFiles(directory="static"), name="static")

# Serve outputs (html reports)
OUT_DIR = Path("outputs")
OUT_DIR.mkdir(exist_ok=True)
app.mount("/outputs", StaticFiles(directory=str(OUT_DIR)), name="outputs")

# ---------------- simple in-memory scan state ----------------
STATE = {
    "running": False,
    "scan_id": None,
    "log": [],
    "subscribers": {},  # scan_id -> list of queues
}
LOCK = threading.Lock()


def push_log(scan_id: str, line: str):
    with LOCK:
        STATE["log"].append(line)
        subs = STATE["subscribers"].get(scan_id, [])
    for q in subs:
        q.append(("log", {"line": line}))


def push_row(scan_id: str, row: dict):
    with LOCK:
        subs = STATE["subscribers"].get(scan_id, [])
    for q in subs:
        q.append(("row", row))


@app.post("/clear_log")
def clear_log():
    with LOCK:
        STATE["log"] = []
    return {"ok": True}


@app.post("/run_scan")
def run_scan(mode: str = "auto", ortex: str = "auto"):
    # Normalize mode from UI: auto|day|night
    if mode not in ("auto", "day", "night"):
        mode = "auto"

    # auto => choose day vs night based on time (simple)
    if mode == "auto":
        # day = regular + premarket; night = afterhours + closed
        # keep it simple: during 7:30–16:00 CT => day, else night
        import datetime
        from zoneinfo import ZoneInfo

        ct = ZoneInfo("America/Chicago")
        now = datetime.datetime.now(tz=ct)
        hhmm = now.hour * 60 + now.minute
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
            push_log(scan_id, "Starting scan… (manual)")
            html_path = scanner.run_scan(
                log_fn=lambda s: push_log(scan_id, s),
                row_fn=lambda r: push_row(scan_id, r),
                mode=mode,
                ortex=ortex,
            )
            if html_path:
                push_log(scan_id, f"Saved HTML: {html_path}")
                done_payload = {"ok": True, "report_url": "/" + html_path}
            else:
                done_payload = {"ok": True, "report_url": None}
        except Exception as e:
            push_log(scan_id, f"[ERROR] {type(e).__name__}: {e}")
            done_payload = {"ok": False, "report_url": None}
        finally:
            with LOCK:
                STATE["running"] = False
            # send done event
            with LOCK:
                subs = STATE["subscribers"].get(scan_id, [])
            for q in subs:
                q.append(("done", done_payload))

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "scan_id": scan_id, "mode": mode}


@app.get("/stream/{scan_id}")
def stream(scan_id: str):
    # SSE stream: yields "log", "row", "done"
    q = []

    with LOCK:
        if scan_id not in STATE["subscribers"]:
            STATE["subscribers"][scan_id] = []
        STATE["subscribers"][scan_id].append(q)

        # replay existing log so UI isn't empty
        existing = list(STATE["log"])

    def gen():
        # replay
        for line in existing:
            yield f"event: log\ndata: {scanner.json_dumps({'line': line})}\n\n"

        # live
        while True:
            if q:
                ev, payload = q.pop(0)
                yield f"event: {ev}\ndata: {scanner.json_dumps(payload)}\n\n"
                if ev == "done":
                    break
            else:
                # keep-alive
                yield "event: ping\ndata: {}\n\n"
                import time
                time.sleep(1)

    return StreamingResponse(gen(), media_type="text/event-stream")
