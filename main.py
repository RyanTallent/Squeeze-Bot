import os
import sys
import subprocess
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()

# Serve UI
app.mount("/static", StaticFiles(directory="static"), name="static")

# Serve generated reports
Path("outputs").mkdir(exist_ok=True)
app.mount("/outputs", StaticFiles(directory="outputs"), name="outputs")


@app.get("/")
def home():
    return FileResponse("static/index.html")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/run_scan")
def run_scan():
    run_id = str(int(time.time()))
    out_path = f"outputs/run_{run_id}.log"

    # Start scanner in background and stream logs to file
    with open(out_path, "w") as f:
        subprocess.Popen(
            [
                sys.executable,
                "-u",
                "scanner.py",
                "--mode",
                "oneshot",
                "--force",
                "--run-id",
                run_id,
            ],
            stdout=f,
            stderr=subprocess.STDOUT,
        )

    return {
        "status": "started",
        "run_id": run_id,
        "log_url": f"/scan_log/{run_id}",
    }


@app.get("/scan_log/{run_id}")
def scan_log(run_id: str):
    path = f"outputs/run_{run_id}.log"

    if not os.path.exists(path):
        return {"status": "missing", "log": ""}

    with open(path, "r", errors="ignore") as f:
        text = f.read()

    return {"status": "ok", "log": text[-12000:]}
