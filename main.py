import os
import sys
import subprocess
import time

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()

# Serve anything in /static (like index.html, favicon later, etc.)
app.mount("/static", StaticFiles(directory="static"), name="static")

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

    os.makedirs("outputs", exist_ok=True)

    with open(out_path, "w") as f:
        subprocess.Popen(
            [sys.executable, "scanner.py"],
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
        return {"status": "missing"}

    with open(path, "r", errors="ignore") as f:
        text = f.read()

    return {
        "status": "ok",
        "log": text[-12000:]
    }
