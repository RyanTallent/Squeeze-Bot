# main.py
import os
import threading
import uuid
import time
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from scanner import run_one_scan  # <-- uses your scanner.py

# ------------------------------------------------------------
# App
# ------------------------------------------------------------
app = FastAPI()

# Serve your one-page UI
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/outputs", StaticFiles(directory="outputs"), name="outputs")


# ------------------------------------------------------------
# In-memory run state
# ------------------------------------------------------------
RUNS = {}  # run_id -> {"log": str, "done": bool, "started": iso, "ended": iso, "html_path": str|None}
RUN_LOCK = threading.Lock()

LATEST_HTML = None
LATEST_HTML_LOCK = threading.Lock()

SCAN_LOCK = threading.Lock()  # prevents overlapping scans


def _append_log(run_id: str, msg: str):
    with RUN_LOCK:
        if run_id not in RUNS:
            RUNS[run_id] = {"log": "", "done": False, "started": None, "ended": None, "html_path": None}
        RUNS[run_id]["log"] += msg + "\n"


def _set_done(run_id: str, html_path: str | None):
    global LATEST_HTML
    with RUN_LOCK:
        RUNS[run_id]["done"] = True
        RUNS[run_id]["ended"] = datetime.now(timezone.utc).isoformat()
        RUNS[run_id]["html_path"] = html_path

    if html_path:
        # html_path will look like "outputs/scan_...html"
        with LATEST_HTML_LOCK:
            LATEST_HTML = "/" + html_path.replace("\\", "/")  # served via /outputs mount


def _run_scan_job(run_id: str):
    # Prevent overlapping scans (manual click + scheduler at same time)
    if not SCAN_LOCK.acquire(blocking=False):
        _append_log(run_id, "Scan skipped: another scan is already running.")
        _set_done(run_id, None)
        return

    try:
        with RUN_LOCK:
            RUNS[run_id] = {
                "log": "",
                "done": False,
                "started": datetime.now(timezone.utc).isoformat(),
                "ended": None,
                "html_path": None,
            }

        def logger(msg: str):
            _append_log(run_id, str(msg))

        logger("Starting scan…")
        html_path = run_one_scan(logger=logger)  # returns like "outputs/scan_....html"
        logger(f"Saved HTML: {html_path}")

        _set_done(run_id, html_path)

    except Exception as e:
        _append_log(run_id, f"ERROR: {e}")
        _set_done(run_id, None)
    finally:
        SCAN_LOCK.release()


# ------------------------------------------------------------
# Market schedule (Central Time by offset-safe ZoneInfo)
# ------------------------------------------------------------
try:
    from zoneinfo import ZoneInfo
    CT = ZoneInfo("America/Chicago")
except Exception:
    CT = timezone(timedelta(hours=-6))


def _is_weekday_ct(dt: datetime) -> bool:
    return dt.weekday() < 5


def _ct_now() -> datetime:
    return datetime.now(tz=CT)


def _time_in_range(dt: datetime, start_hm: tuple[int, int], end_hm: tuple[int, int]) -> bool:
    """start <= dt.time < end (same day)."""
    s = dt.replace(hour=start_hm[0], minute=start_hm[1], second=0, microsecond=0)
    e = dt.replace(hour=end_hm[0], minute=end_hm[1], second=0, microsecond=0)
    return s <= dt < e


def _schedule_interval_seconds(dt_ct: datetime) -> int:
    """
    Your rule:
      - every 5 minutes in pre + regular hours
      - every 30 minutes in post hours

    We'll define:
      PRE+REG = 03:00–16:00 CT
      POST    = 16:00–20:00 CT
      else: sleep longer
    """
    if not _is_weekday_ct(dt_ct):
        return 15 * 60  # weekends: check occasionally

    if _time_in_range(dt_ct, (3, 0), (16, 0)):
        return 5 * 60

    if _time_in_range(dt_ct, (16, 0), (20, 0)):
        return 30 * 60

    return 15 * 60


def _scheduler_loop():
    """
    Runs forever as long as the web service is running.
    """
    while True:
        try:
            dt = _ct_now()
            interval = _schedule_interval_seconds(dt)

            # Only auto-scan on weekdays during pre/reg/post windows
            should_scan = _is_weekday_ct(dt) and (
                _time_in_range(dt, (3, 0), (20, 0))
            )

            if should_scan:
                run_id = "auto-" + uuid.uuid4().hex[:10]
                _append_log(run_id, f"[AUTO] Trigger at {dt.strftime('%Y-%m-%d %H:%M:%S %Z')}")
                _run_scan_job(run_id)

            time.sleep(interval)

        except Exception:
            # If scheduler fails, don't die — wait and keep going
            time.sleep(60)


@app.on_event("startup")
def _startup():
    # Start scheduler in background thread
    t = threading.Thread(target=_scheduler_loop, daemon=True)
    t.start()


# ------------------------------------------------------------
# Routes
# ------------------------------------------------------------
@app.get("/")
def home():
    return FileResponse("static/index.html")


@app.post("/run_scan")
def run_scan():
    run_id = uuid.uuid4().hex[:10]
    thread = threading.Thread(target=_run_scan_job, args=(run_id,), daemon=True)
    thread.start()
    return {"run_id": run_id, "log_url": f"/scan_log/{run_id}"}


@app.get("/scan_log/{run_id}")
def scan_log(run_id: str):
    with RUN_LOCK:
        data = RUNS.get(run_id)

    if not data:
        return JSONResponse({"log": "No log found for this run_id.", "done": True}, status_code=404)

    with LATEST_HTML_LOCK:
        latest = LATEST_HTML

    return {
        "log": data.get("log", ""),
        "done": bool(data.get("done")),
        "latest_html": latest,
    }


@app.get("/latest_report")
def latest_report():
    with LATEST_HTML_LOCK:
        latest = LATEST_HTML
    return {"latest_html": latest}
