import os
import sys
import subprocess
import time

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()

# Serve static files (index.html, etc.)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def home():
    return FileResponse("static/index.html")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/run_scan")
def run_scan():
    # Unique id for this run
    run_id = str(int(time.time()))
    out_path = f"outputs/run_{run_id}.log"

    # Ensure outputs folder exists
    os.makedirs("outputs", exist_ok=True)

    # Start scanner in the background.
    # -u makes output unbuffered so logs show up live.
    with open(out_path, "w") as f:
        subprocess.Popen(
            [sys.executable, "-u", "scanner.py"],
            stdout=f,
            stderr=subprocess.STDOUT
        )

    return {
        "status": "started",
        "run_id": run_id,
        "log_url": f"/scan_log/{run_id}"
    }


@app.get("/scan_log/{run_id}")
def scan_log(run_id: str):
    path = f"outputs/run_{run_id}.log"

    if not os.path.exists(path):
        return {"status": "missing", "log": ""}

    with open(path, "r", errors="ignore") as f:
        text = f.read()

    return {"status": "ok", "log": text[-12000:]}
