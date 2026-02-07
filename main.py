# main.py
import os
import threading
import traceback
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse, FileResponse
from fastapi.staticfiles import StaticFiles

import scanner  # scanner.py

app = FastAPI()

# --- outputs directory ---
OUT_DIR = "outputs"
os.makedirs(OUT_DIR, exist_ok=True)
app.mount("/outputs", StaticFiles(directory=OUT_DIR), name="outputs")

# --- persistent log file ---
LOG_PATH = os.path.join(OUT_DIR, "live_log.txt")

SCAN_LOCK = threading.Lock()
SCAN_RUNNING = False
LAST_REPORT_PATH: str | None = None


def _append_log_line(line: str):
    # append to file so it survives restarts
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def log(msg: str):
    # Use UTC timestamp for server log line, keep your message content the same
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    _append_log_line(line)


def clear_log():
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        f.write("")


def do_scan():
    global SCAN_RUNNING, LAST_REPORT_PATH
    try:
        log("Scanner thread started. Calling scanner.run_scan()...")

        html_path = scanner.run_scan(log_fn=log)

        if html_path:
            LAST_REPORT_PATH = html_path.replace("\\", "/")
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
    .left { width: 420px; }
    .card { border: 1px solid #e6e6e6; border-radius: 12px; padding: 12px; margin-bottom: 12px; }
    button { padding: 10px 14px; border-radius: 10px; border: 1px solid #ddd; cursor: pointer; }
    pre { height: 520px; overflow: auto; background: #0b0f14; color: #cfe3ff; padding: 10px; border-radius: 10px; user-select: text; }
    iframe { width: 100%; height: 700px; border: 1px solid #e6e6e6; border-radius: 12px; }
    .muted { color: #666; font-size: 13px; }
    .status { margin-top: 8px; }
    a { color: #0b65d8; text-decoration: none; font-weight: 700; }
  </style>
</head>
<body>
  <h2>Squeeze Bot</h2>
  <div class="muted">Click Run Scan. Log updates live. Latest report loads on the right (same tab).</div>

  <div class="row">
    <div class="left">
      <div class="card">
        <button id="runBtn">Run Scan</button>
        <button id="clearBtn" style="margin-left:8px;">Clear Log</button>
        <div id="status" class="muted status"></div>
        <div style="margin-top:10px;">
          <a id="openReport" href="#" target="_blank" style="display:none;">Open latest HTML report</a>
        </div>
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
  const logEl = document.getElementById("log");
  const statusEl = document.getElementById("status");
  const runBtn = document.getElementById("runBtn");
  const clearBtn = document.getElementById("clearBtn");
  const reportFrame = document.getElementById("report");
  const openReport = document.getElementById("openReport");

  let pausePoll = false;

  logEl.addEventListener("mousedown", () => { pausePoll = true; });
  window.addEventListener("mouseup", () => { setTimeout(() => { pausePoll = false; }, 400); });

  function extractLatestHtmlPath(txt) {
    const matches = txt.match(/Saved HTML:\\s*(outputs\\/[^\\s]+\\.html)/g);
    if (!matches || matches.length === 0) return null;
    const last = matches[matches.length - 1].replace("Saved HTML:", "").trim();
    return "/" + last; // outputs mounted at /outputs
  }

  async function runScan(){
    statusEl.textContent = "Starting...";
    try {
      const res = await fetch("/run_scan", { method: "POST" });
      if (!res.ok) {
        statusEl.textContent = "Run failed (server returned error).";
        return;
      }
      const data = await res.json().catch(() => ({}));
      statusEl.textContent = (data && data.running) ? "Already running (check log)..." : "Running (check log)...";
    } catch (e) {
      statusEl.textContent = "Run failed (network error).";
    }
  }

  async function clearLog(){
    statusEl.textContent = "Clearing log...";
    try {
      const res = await fetch("/clear_log", { method: "POST" });
      statusEl.textContent = res.ok ? "Log cleared." : "Failed to clear log.";
    } catch (e) {
      statusEl.textContent = "Failed to clear log.";
    }
  }

  runBtn.addEventListener("click", runScan);
  clearBtn.addEventListener("click", clearLog);

  async function poll(){
    try{
      const r = await fetch("/scan_log", { cache: "no-store" });
      const txt = await r.text();

      if (!pausePoll) {
        logEl.textContent = txt || "No log yet. Click Run Scan.";
        logEl.scrollTop = logEl.scrollHeight;

        const reportUrl = extractLatestHtmlPath(txt);
        if (reportUrl) {
          openReport.href = reportUrl;
          openReport.style.display = "inline";
          reportFrame.src = reportUrl + "?ts=" + Date.now();
        }
      }
    }catch(e){}
    setTimeout(poll, 1500);
  }

  poll();
</script>
</body>
</html>
"""


# Helps Render health checks that send HEAD /
@app.head("/")
def head_root():
    return PlainTextResponse("ok")


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


@app.post("/clear_log")
def clear_log_endpoint():
    clear_log()
    return {"ok": True}


@app.get("/scan_log", response_class=PlainTextResponse)
def scan_log():
    if not os.path.exists(LOG_PATH):
        return ""
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


@app.get("/report")
def report():
    if not LAST_REPORT_PATH:
        return HTMLResponse("<div style='font-family:Arial;padding:16px;color:#666'>No report yet. Click Run Scan.</div>")

    path = LAST_REPORT_PATH
    if not os.path.exists(path):
        return HTMLResponse(f"<div style='font-family:Arial;padding:16px;color:#666'>Report missing: {path}</div>")

    return FileResponse(path, media_type="text/html")
