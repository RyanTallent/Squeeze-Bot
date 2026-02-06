# main.py
import threading
import time
import uuid
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import scanner

app = FastAPI()

# Serve your single-page UI
app.mount("/static", StaticFiles(directory="static"), name="static")

# Serve outputs (html reports, csv, json, txt)
OUT_DIR = Path("outputs")
OUT_DIR.mkdir(exist_ok=True)
app.mount("/outputs", StaticFiles(directory=str(OUT_DIR)), name="outputs")

# ---------- simple in-memory state ----------
STATE_LOCK = threading.Lock()
STATE = {
    "running": False,
    "log": "",
    "latest_html": None,
    "last_run_id": None,
}

def append_log(line: str):
    with STATE_LOCK:
        STATE["log"] += line.rstrip() + "\n"
        # keep log from growing forever
        if len(STATE["log"]) > 200_000:
            STATE["log"] = STATE["log"][-200_000:]


def run_scan_job(run_id: str, reason: str):
    with STATE_LOCK:
        STATE["running"] = True
        STATE["last_run_id"] = run_id
        STATE["log"] = ""
    append_log(f"Starting scan… ({reason})")
    try:
        html_path = scanner.run_scan(log_fn=append_log)
        if html_path:
            # html_path is like outputs/scan_...html
            append_log(f"Saved HTML: {html_path}")
            with STATE_LOCK:
                STATE["latest_html"] = "/" + html_path.replace("\\", "/")
        else:
            append_log("Scan finished but no HTML was generated.")
    except Exception as e:
        append_log(f"Scan crashed: {e}")
    finally:
        with STATE_LOCK:
            STATE["running"] = False


@app.get("/", response_class=HTMLResponse)
def home():
    # Serve the single tab UI
    index = Path("static/index.html")
    return index.read_text(encoding="utf-8")


@app.post("/run_scan")
def run_scan():
    with STATE_LOCK:
        if STATE["running"]:
            return JSONResponse({"ok": False, "message": "Scan already running", "log_url": "/scan_log"})
        run_id = str(uuid.uuid4())

    t = threading.Thread(target=run_scan_job, args=(run_id, "manual"), daemon=True)
    t.start()
    return JSONResponse({"ok": True, "run_id": run_id, "log_url": "/scan_log"})


@app.get("/scan_log")
def scan_log():
    with STATE_LOCK:
        return JSONResponse({
            "running": STATE["running"],
            "log": STATE["log"],
            "latest_html": STATE["latest_html"],
        })


# ---------- background scheduler ----------
def scheduler_loop():
    # Runs forever, decides scan cadence based on CT time (inside scanner module)
    while True:
        try:
            interval = scanner.next_interval_seconds()
            # If interval is None, we're "closed" (sleep longer)
            if interval is None:
                time.sleep(60 * 10)
                continue

            # If not running, start an auto scan
            with STATE_LOCK:
                can_start = not STATE["running"]

            if can_start:
                run_id = str(uuid.uuid4())
                t = threading.Thread(target=run_scan_job, args=(run_id, f"auto every {interval//60}m"), daemon=True)
                t.start()

            time.sleep(interval)
        except Exception:
            time.sleep(30)


@app.on_event("startup")
def on_startup():
    # Start scheduler in background
    threading.Thread(target=scheduler_loop, daemon=True).start()
