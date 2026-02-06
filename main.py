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
    # Import here so the app can still boot even if scanner has optional deps
    import scanner

    # If your scanner file has a main function, use it.
    # Most likely it runs when executed directly, so we call a function if present.
    if hasattr(scanner, "run_scan"):
        results = scanner.run_scan()
        return {"status": "ok", "results": results}

    # Fallback: run the script-style scanner and capture output
    import subprocess, sys
    proc = subprocess.run([sys.executable, "scanner.py"], capture_output=True, text=True, timeout=300)
    return {
        "status": "ok" if proc.returncode == 0 else "error",
        "stdout": proc.stdout[-8000:],  # last chunk so response isn't huge
        "stderr": proc.stderr[-8000:],
        "code": proc.returncode,
    }
