# scanner.py
import os
import json
import math
import time
import requests
from pathlib import Path
from datetime import datetime, timedelta, timezone

# ===============================
# CONFIG
# ===============================
PM_START = (3, 0)
PM_END   = (8, 29)
REG_START = (8, 30)
REG_END   = (15, 0)
AH_START  = (15, 0)
AH_END    = (19, 0)

MAX_FINALISTS = 75
MIN_DOLLAR_VOL = 150_000

POLYGON_KEY = os.getenv("POLYGON_API_KEY")

OUT_DIR = Path("outputs")
OUT_DIR.mkdir(exist_ok=True)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "squeeze-bot"})

CT = timezone(timedelta(hours=-6))

# ===============================
# UTILS
# ===============================
def json_dumps(x):
    return json.dumps(x, default=str)

def now_ct():
    return datetime.now(CT)

def ct_dt(d, hm):
    return datetime(d.year, d.month, d.day, hm[0], hm[1], tzinfo=CT)

def market_window(dt):
    pm_s, pm_e = ct_dt(dt, PM_START), ct_dt(dt, PM_END)
    rg_s, rg_e = ct_dt(dt, REG_START), ct_dt(dt, REG_END)
    ah_s, ah_e = ct_dt(dt, AH_START), ct_dt(dt, AH_END)

    if pm_s <= dt <= pm_e:
        return "PREMARKET", pm_s, dt
    if rg_s <= dt <= rg_e:
        return "REGULAR", rg_s, dt
    if ah_s <= dt <= ah_e:
        return "AFTERHOURS", ah_s, dt

    # CLOSED → last after-hours
    if dt > ah_e:
        return "AFTERHOURS (LAST)", ah_s, ah_e

    y = dt - timedelta(days=1)
    return "AFTERHOURS (LAST)", ct_dt(y, AH_START), ct_dt(y, AH_END)

def polygon_get(url, params=None):
    params = params or {}
    params["apiKey"] = POLYGON_KEY
    r = SESSION.get(url, params=params, timeout=20)
    if r.status_code == 429:
        time.sleep(1.5)
        return None
    if r.status_code >= 400:
        return None
    return r.json()

# ===============================
# CORE ANALYSIS
# ===============================
def analyze_ticker(ticker, date_str, w_start, w_end):
    aggs = polygon_get(
        f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/minute/{date_str}/{date_str}",
        {"adjusted": "true", "sort": "asc", "limit": 50000}
    )
    if not aggs or "results" not in aggs:
        return None

    bars = []
    for r in aggs["results"]:
        t = datetime.fromtimestamp(r["t"]/1000, timezone.utc).astimezone(CT)
        if w_start <= t <= w_end:
            bars.append(r)

    if len(bars) < 3:
        return None

    o = bars[0]["o"]
    h = max(b["h"] for b in bars)
    l = min(b["l"] for b in bars)
    c = bars[-1]["c"]
    v = sum(b["v"] for b in bars)

    dollar_vol = v * c
    if dollar_vol < MIN_DOLLAR_VOL:
        return None

    range_pct = (h - l) / max(l, 1e-6)
    hold_pct = (c - l) / max(h - l, 1e-6)
    move_pct = (c - o) / max(o, 1e-6)

    window_minutes = int((w_end - w_start).total_seconds() // 60)

    score = (
        min(range_pct * 120, 40) +
        min(hold_pct * 60, 30) +
        min(math.log10(dollar_vol + 1) * 5, 30)
    )

    confidence = max(1, min(10, round(score / 10)))

    subtype = "momentum breakout" if hold_pct > 0.6 else "momentum pullback"

    return {
        "ticker": ticker,
        "confidence": confidence,
        "bucket": "MOMENTUM",
        "subtype": subtype,
        "close": round(c, 2),
        "move_pct": round(move_pct * 100, 1),
        "dollar_vol": int(dollar_vol),
        "range_pct": round(range_pct * 100, 1),
        "hold_strength_pct": round(hold_pct * 100),
        "window_minutes": window_minutes,
        "window_label": f"{w_start.strftime('%H:%M')}–{w_end.strftime('%H:%M')} CT",
        "trigger": round(h, 4),
        "stop": round(l, 4),
        "plan": f"Entry near trigger {h:.4f}. Stop {l:.4f}. Avoid chasing >2% above trigger."
    }

# ===============================
# ENTRYPOINT
# ===============================
def run_scan(log_fn=None, row_fn=None):
    dt = now_ct()
    window, w_start, w_end = market_window(dt)
    date_str = w_start.strftime("%Y-%m-%d")

    if log_fn:
        log_fn(f"Market window: {window} ({w_start.strftime('%H:%M')}–{w_end.strftime('%H:%M')} CT)")

    snap = polygon_get("https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers")
    if not snap:
        return None

    tickers = [t["ticker"] for t in snap.get("tickers", [])][:MAX_FINALISTS]

    results = []
    for i, t in enumerate(tickers, 1):
        r = analyze_ticker(t, date_str, w_start, w_end)
        if r:
            results.append(r)
            if row_fn:
                row_fn(r)
        if log_fn and i % 10 == 0:
            log_fn(f"Analyzing {i}/{len(tickers)}...")

    # Write report
    ts = now_ct().strftime("%Y-%m-%d_%H-%M-%S")
    html_path = OUT_DIR / f"scan_{ts}.html"

    with open(html_path, "w") as f:
        f.write("<html><body><h2>Squeeze Bot Results</h2><pre>")
        f.write(json.dumps(results, indent=2))
        f.write("</pre></body></html>")

    if log_fn:
        log_fn(f"Saved HTML: {html_path}")

    return str(html_path)
