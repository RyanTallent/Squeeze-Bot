import os
import threading
import traceback
from datetime import datetime

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

import scanner  # your scanner.py

app = FastAPI()

# Serve outputs/ so HTML reports can be loaded
os.makedirs("outputs", exist_ok=True)
app.mount("/outputs", StaticFiles(directory="outputs"), name="outputs")

# --- Global state ---
LOG_LINES: list[str] = []
SCAN_LOCK = threading.Lock()
SCAN_RUNNING = False
LAST_REPORT_PATH: str | None = None


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    LOG_LINES.append(line)
    # keep log from growing forever
    if len(LOG_LINES) > 800:
        del LOG_LINES[:200]


def do_scan():
    global SCAN_RUNNING, LAST_REPORT_PATH
    try:
        log("Scanner thread started. Calling scanner.run_scan()...")

        # This will run your scan and should call log_fn for progress
        html_path = scanner.run_scan(log_fn=log)

        if html_path:
            LAST_REPORT_PATH = html_path.replace("\\", "/")
            log(f"Saved HTML: {LAST_REPORT_PATH}")
        else:
            log("Scan finished but returned no HTML path.")

    except Exception:
        log("SCAN CRASHED — traceback below:")
        tb = traceback.format_exc()
        for line in tb.splitlines():
            log(line)

    finally:
        with SCAN_LOCK:
            SCAN_RUNNING = False
        log("Scanner thread finished.")


@app.get("/", response_class=HTMLResponse)
def home():
    return """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Squeeze Bot</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 18px; }
    .row { display: flex; gap: 14px; }
    .left { width: 360px; }
    .card { border: 1px solid #e6e6e6; border-radius: 12px; padding: 12px; margin-bottom: 12px; }
    button { padding: 10px 14px; border-radius: 10px; border: 1px solid #ddd; cursor: pointer; }
    pre { height: 520px; overflow: auto; background: #0b0f14; color: #cfe3ff; padding: 10px; border-radius: 10px; }
    iframe { width: 100%; height: 700px; border: 1px solid #e6e6e6; border-radius: 12px; }
    .muted { color: #666; font-size: 13px; }
  </style>
</head>
<body>
  <h2>Squeeze Bot</h2>
  <div class="muted">Click Run Scan. Log updates live. Latest report loads on the right (same tab).</div>

  <div class="row">
    <div class="left">
      <div class="card">
        <button id="runBtn">Run Scan</button>
        <div id="status" class="muted" style="margin-top:8px;"></div>
      </div>

      <div class="card">
        <div class="muted">Live Log (click/drag to copy — polling pauses while mouse is down)</div>
        <pre id="log"></pre>
      </div>
    </div>

    <div style="flex:1;">
      <div class="card">
        <div class="muted">Latest Report</div>
        <iframe id="report" src="/report"></iframe>
      </div>
    </div>
  </div>

<script>
let pausePoll = false;

const logEl = document.getElementById("log");
const reportEl = document.getElementById("report");
const statusEl = document.getElementById("status");
const runBtn = document.getElementById("runBtn");

// Pause polling while user is selecting/copying text in the log
logEl.addEventListener("mousedown", () => { pausePoll = true; });
logEl.addEventListener("mouseup", () => { setTimeout(() => { pausePoll = false; }, 500); });

async function runScan(){
  statusEl.innerText = "Starting...";
  try{
    const res = await fetch("/run_scan", { method: "POST" });
    const data = await res.json().catch(() => ({}));
    if (data && data.running) {
      statusEl.innerText = "Already running (check log)...";
    } else {
      statusEl.innerText = "Running (check log)...";
    }
  }catch(e){
    statusEl.innerText = "Error starting scan (see console)";
    console.error(e);
  }
}

runBtn.addEventListener("click", runScan);

async function poll(){
  try{
    const r = await fetch("/scan_log");
    const txt = await r.text();

    if (!pausePoll) {
      logEl.textContent = txt;

      // If we see "Saved HTML:" then reload iframe
      if (txt.includes("Saved HTML:")) {
        reportEl.src = "/report?ts=" + Date.now();
      }
    }
  }catch(e){
    // ignore poll errors; keep polling
  }
  setTimeout(poll, 1000);
}

poll();
</script>
</body>
</html>
"""


@app.post("/run_scan")
def run_scan():
    global SCAN_RUNNING
    with SCAN_LOCK:
        if SCAN_RUNNING:
            log("Scan requested but one is already running.")
            return {"ok": True, "running": True}

        SCAN_RUNNING = True

    log("Starting scan… (manual)")
    t = threading.Thread(target=do_scan, daemon=True)
    t.start()
    return {"ok": True, "started": True}


@app.get("/scan_log", response_class=PlainTextResponse)
def scan_log():
    return "\n".join(LOG_LINES) + ("\n" if LOG_LINES else "")


@app.get("/report")
def report():
    if not LAST_REPORT_PATH:
        return HTMLResponse(
            "<div style='font-family:Arial;padding:16px;color:#666'>No report yet. Click Run Scan.</div>"
        )

    path = LAST_REPORT_PATH
    if not os.path.exists(path):
        return HTMLResponse(
            f"<div style='font-family:Arial;padding:16px;color:#666'>Report missing: {path}</div>"
        )

    return FileResponse(path, media_type="text/html")
