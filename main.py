# main.py
import uuid
import queue
import threading
import traceback
from pathlib import Path
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse

import scanner

app = FastAPI()

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
OUT_DIR = BASE_DIR / "outputs"
OUT_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/outputs", StaticFiles(directory=str(OUT_DIR)), name="outputs")

LOG_PATH = OUT_DIR / "live_log.txt"

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


@app.get("/")
def home():
    # Always serve your SPA from /static (consistent paths)
    return RedirectResponse(url="/static/index.html")


@app.post("/run_scan")
def run_scan():
    """
    Starts a scan and returns scan_id.
    UI opens EventSource(/stream/{scan_id})
    """
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
    """
    Server-Sent Events stream: logs + rows + done.
    """
    with STATE_LOCK:
        s = SCANS.get(scan_id)
        if not s:
            return JSONResponse({"ok": False, "error": "Unknown scan_id"}, status_code=404)
        q = s["q"]

    def gen():
        # initial heartbeat so proxies open stream
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
        "X-Accel-Buffering": "no",  # helps with proxy buffering
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
