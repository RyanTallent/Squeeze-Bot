import os
from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def home():
    return {"message": "Squeeze_Bot is live"}

@app.get("/health")
def health():
    return {"status": "ok"}

# We'll connect this to your scanner next
@app.post("/run_scan")
def run_scan():
    return {"status": "ready", "note": "Hook scanner here next"}
