from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()

# Serve anything in /static (like index.html, favicon later, etc.)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def home():
    # Serve the HTML homepage
    return FileResponse("static/index.html")

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/run_scan")
def run_scan():
    return {"status": "ready"}
