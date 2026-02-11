# scanner.py
import os
import json
import math
import time
import requests
from pathlib import Path
from datetime import datetime, timedelta, timezone

# ============================================================
# ENGINE V2.4 — ORTEX TOGGLE + STREAM ONLY FINAL TOP 5
# - top 5 squeeze + top 5 momentum
# - squeeze tab always filled (true squeezes OR squeeze watch)
# - ortex toggle: auto|on|off (requested) + actual on/off
# ============================================================

# UI sizing
TOP_N_PER_BUCKET = 5

# DAY mode (deeper)
DEEP_ANALYZE_TOP_DAY = 75
ORTEX_FINALISTS_DAY = 25

# NIGHT mode (cheaper)
DEEP_ANALYZE_TOP_NIGHT = 35
ORTEX_FINALISTS_NIGHT = 0

# Snapshot filter (cheap “whole market” pass)
MIN_DOLLAR_VOL_PROXY = 250_000
MIN_MOVE_PROXY_PCT = 0.01

# Price tiers
PRICE_TIERS = [(0.01, 2.50), (2.50, 5.00), (5.00, 10.00)]
MAX_CANDIDATES_PER_TIER_SNAPSHOT = 600

# Market hours (Central Time)
PM_START_CT = (3, 0)
PM_END_CT   = (8, 29)

REG_START_CT = (8, 30)
REG_END_CT   = (15, 0)

AH_START_CT = (15, 0)
AH_END_CT   = (19, 0)

# ORTEX active full trading session including premarket
ORTEX_ON_START_CT = (3, 0)   # 3:00 AM CT (premarket open)
ORTEX_ON_END_CT   = (16, 0)  # 4:00 PM CT

# Keys
POLYGON_KEY = os.getenv("POLYGON_API_KEY")
ORTEX_KEY = os.getenv("ORTEX_API_KEY")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "squeeze-bot/engine-v2.4"})

OUT_DIR = Path("outputs")
OUT_DIR.mkdir(exist_ok=True)

# Timezone (Central)
try:
    from zoneinfo import ZoneInfo
    CT_TZ = ZoneInfo("America/Chicago")
except Exception:
    CT_TZ = timezone(timedelta(hours=-6))


# ============================================================
# Small utils
# ============================================================
def json_dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)

def now_ct() -> datetime:
    return datetime.now(tz=CT_TZ)

def ct_date_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")

def ct_dt(date_str: str, hm: tuple[int, int]) -> datetime:
    y, m, d = map(int, date_str.split("-"))
    return datetime(y, m, d, hm[0], hm[1], tzinfo=CT_TZ)

def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))

def safe_float(x):
    try:
        return None if x is None else float(x)
    except Exception:
        return None

def safe_int(x):
    try:
        return None if x is None else int(float(x))
    except Exception:
        return None

def ms_to_utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)

def sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)

def score_to_prob(score: float) -> float:
    return sigmoid((score - 55.0) / 12.0)


# ============================================================
# Market window logic
# ============================================================
def current_market_window(dt: datetime) -> tuple[str, datetime, datetime]:
    """
    Returns (window_name, start_local, end_local) in CT.
    If market is CLOSED, returns most recent AFTERHOURS session (15:00–19:00 CT).
    """
    if dt.weekday() >= 5:
        back = 1 if dt.weekday() == 5 else 2
        y = dt - timedelta(days=back)
        y_str = ct_date_str(y)
        y_ah_start = ct_dt(y_str, AH_START_CT)
        y_ah_end = ct_dt(y_str, AH_END_CT)
        return ("AFTERHOURS (LAST)", y_ah_start, y_ah_end)

    date_str = ct_date_str(dt)
    pm_start = ct_dt(date_str, PM_START_CT)
    pm_end = ct_dt(date_str, PM_END_CT) + timedelta(seconds=59)

    reg_start = ct_dt(date_str, REG_START_CT)
    reg_end = ct_dt(date_str, REG_END_CT)

    ah_start = ct_dt(date_str, AH_START_CT)
    ah_end = ct_dt(date_str, AH_END_CT)

    if pm_start <= dt <= pm_end:
        return ("PREMARKET", pm_start, dt)

    if reg_start <= dt <= reg_end:
        return ("REGULAR", reg_start, dt)

    if ah_start <= dt <= ah_end:
        return ("AFTERHOURS", ah_start, dt)

    if dt > ah_end:
        return ("AFTERHOURS (LAST)", ah_start, ah_end)

    y = dt - timedelta(days=1)
    while y.weekday() >= 5:
        y -= timedelta(days=1)
    y_str = ct_date_str(y)
    y_ah_start = ct_dt(y_str, AH_START_CT)
    y_ah_end = ct_dt(y_str, AH_END_CT)
    return ("AFTERHOURS (LAST)", y_ah_start, y_ah_end)

def ortex_allowed_now(dt: datetime) -> bool:
    if dt.weekday() >= 5:
        return False
    d = ct_date_str(dt)
    start = ct_dt(d, ORTEX_ON_START_CT)
    end = ct_dt(d, ORTEX_ON_END_CT)
    return start <= dt <= end


# ============================================================
# Polygon
# ============================================================
def polygon_get(url: str, params: dict | None = None, log_fn=None, retries: int = 3) -> dict:
    if not POLYGON_KEY:
        raise RuntimeError("POLYGON_API_KEY environment variable is not set")

    params = dict(params or {})
    params["apiKey"] = POLYGON_KEY

    for attempt in range(retries + 1):
        try:
            r = SESSION.get(url, params=params, timeout=30)

            if r.status_code == 429:
                wait = 1.5 * (2 ** attempt)
                if log_fn:
                    log_fn(f"[POLYGON 429] throttled; wait={wait:.1f}s")
                time.sleep(wait)
                continue

            if r.status_code >= 400:
                if log_fn:
                    log_fn(f"[POLYGON {r.status_code}] {url} {r.text[:160]}")
                r.raise_for_status()

            return r.json()

        except requests.RequestException as e:
            wait = 1.5 * (2 ** attempt)
            if log_fn:
                log_fn(f"[POLYGON EXC] {type(e).__name__}: {str(e)[:120]} wait={wait:.1f}s")
            time.sleep(wait)

    raise RuntimeError(f"Polygon failed after retries: {url}")

def get_snapshot_all_tickers(log_fn=None) -> list[dict]:
    data = polygon_get(
        "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers",
        {},
        log_fn=log_fn
    )
    return data.get("tickers", []) or []

def get_minute_aggs(ticker: str, date_str: str, log_fn=None) -> list[dict]:
    url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/minute/{date_str}/{date_str}"
    data = polygon_get(url, {"adjusted": "true", "sort": "asc", "limit": 50000}, log_fn=log_fn)
    return data.get("results", []) or []

def get_daily_aggs_20d(ticker: str, end_date_str: str, log_fn=None) -> list[dict]:
    end_dt = datetime.fromisoformat(end_date_str)
    start_dt = end_dt - timedelta(days=25)
    start_str = start_dt.strftime("%Y-%m-%d")
    url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{start_str}/{end_date_str}"
    data = polygon_get(url, {"adjusted": "true", "sort": "asc", "limit": 50000}, log_fn=log_fn)
    return data.get("results", []) or []

def polygon_reference(ticker: str, log_fn=None) -> dict | None:
    url = f"https://api.polygon.io/v3/reference/tickers/{ticker}"
    try:
        data = polygon_get(url, {}, log_fn=log_fn)
        return data.get("results") or {}
    except Exception:
        return None


# ============================================================
# ORTEX
# ============================================================
def ortex_get(url: str, log_fn=None) -> dict | None:
    if not ORTEX_KEY:
        return None

    r = SESSION.get(url, headers={"Ortex-Api-Key": ORTEX_KEY}, timeout=30)

    if r.status_code == 404:
        return None
    if r.status_code == 429:
        if log_fn:
            log_fn("[ORTEX 429] throttled — skipping")
        return None
    if r.status_code >= 400:
        if log_fn:
            log_fn(f"[ORTEX {r.status_code}] {url} {r.text[:140]}")
        return None

    try:
        return r.json()
    except Exception:
        return None

def ortex_short_interest_features(ticker: str, log_fn=None) -> dict | None:
    data = ortex_get(f"https://api.ortex.com/api/v1/stock/US/{ticker}/short_interest", log_fn=log_fn)
    if not data:
        return None
    rows = data.get("rows", []) if isinstance(data, dict) else []
    if not rows:
        return None

    latest = rows[-1]
    prev = rows[-2] if len(rows) >= 2 else None

    si_pct = safe_float(latest.get("shortInterestPcFreeFloat"))
    si_shares = safe_float(latest.get("shortInterestShares"))

    si_pct_chg = None
    if prev is not None and si_pct is not None:
        prev_pct = safe_float(prev.get("shortInterestPcFreeFloat"))
        if prev_pct is not None:
            si_pct_chg = si_pct - prev_pct

    return {"si_pct_ff": si_pct, "si_pct_chg": si_pct_chg, "si_shares": si_shares}

def ortex_ctb_latest(ticker: str, log_fn=None) -> float | None:
    for url in [
        f"https://api.ortex.com/api/v1/stock/US/{ticker}/ctb/all",
        f"https://api.ortex.com/api/v1/stock/US/{ticker}/ctb/new",
    ]:
        data = ortex_get(url, log_fn=log_fn)
        if not data:
            continue
        rows = data.get("rows", []) if isinstance(data, dict) else []
        if not rows:
            continue
        latest = rows[-1]
        for k in ("ctbAvg", "ctbAverage", "average", "borrowCostAvg", "borrowCostAverage"):
            v = safe_float(latest.get(k))
            if v is not None:
                return v
    return None

def ortex_availability_latest(ticker: str, log_fn=None) -> float | None:
    data = ortex_get(f"https://api.ortex.com/api/v1/stock/US/{ticker}/availability", log_fn=log_fn)
    if not data:
        return None
    rows = data.get("rows", []) if isinstance(data, dict) else []
    if not rows:
        return None
    latest = rows[-1]
    for k in ("shares", "availableShares", "availabilityShares", "available", "avail"):
        v = safe_float(latest.get(k))
        if v is not None:
            return v
    return None


# ============================================================
# Candidate selection
# ============================================================
def pick_snapshot_candidates(snap: list[dict], price_min: float, price_max: float) -> list[tuple[float, str, dict]]:
    out: list[tuple[float, str, dict]] = []

    for item in snap:
        t = item.get("ticker")
        if not t:
            continue

        last_trade = item.get("lastTrade") or {}
        p = safe_float(last_trade.get("p"))
        if p is None or not (price_min <= p <= price_max):
            continue

        day = item.get("day") or {}
        prev = item.get("prevDay") or {}

        day_vol = safe_float(day.get("v")) or 0.0
        prev_close = safe_float(prev.get("c"))

        move_pct = 0.0
        if prev_close and prev_close > 0:
            move_pct = (p - prev_close) / prev_close

        dollar_vol_proxy = day_vol * p

        if dollar_vol_proxy < MIN_DOLLAR_VOL_PROXY and abs(move_pct) < MIN_MOVE_PROXY_PCT:
            continue

        proxy = (dollar_vol_proxy / 1_000_000.0) + (abs(move_pct) * 6.0)
        out.append((proxy, t, {"prev_close": prev_close}))

    out.sort(reverse=True, key=lambda x: x[0])
    return out[:MAX_CANDIDATES_PER_TIER_SNAPSHOT]


# ============================================================
# Deep analysis
# ============================================================
def compute_rel_vol(window_vol: float, avg_daily_vol: float | None, window_minutes: int) -> float | None:
    if avg_daily_vol is None or avg_daily_vol <= 0:
        return None
    expected = avg_daily_vol * (window_minutes / 390.0)
    if expected <= 0:
        return None
    return window_vol / expected

def analyze_ticker_window(
    ticker: str,
    date_str: str,
    w_start: datetime,
    w_end: datetime,
    snap_meta: dict,
    log_fn=None
) -> dict | None:
    bars = get_minute_aggs(ticker, date_str, log_fn=log_fn)
    if not bars:
        return None

    start_utc = w_start.astimezone(timezone.utc)
    end_utc = w_end.astimezone(timezone.utc)

    window = [b for b in bars if start_utc <= ms_to_utc(b["t"]) <= end_utc]
    if len(window) < 3:
        return None

    o = float(window[0]["o"])
    h = float(max(b["h"] for b in window))
    l = float(min(b["l"] for b in window))
    c = float(window[-1]["c"])
    v = float(sum(b["v"] for b in window))
    dollar_vol = v * c

    rng = h - l
    range_pct = rng / max(l, 1e-9)
    hold_pct = (c - l) / max(rng, 1e-9)

    prev_close = snap_meta.get("prev_close")
    move_pct = 0.0
    if prev_close and prev_close > 0:
        move_pct = (c - prev_close) / prev_close

    daily = get_daily_aggs_20d(ticker, date_str, log_fn=log_fn)
    vols = [safe_float(b.get("v")) for b in daily[-10:]] if daily else []
    vols = [x for x in vols if x is not None]
    adv10 = (sum(vols) / len(vols)) if vols else None

    window_minutes = max(1, int((w_end - w_start).total_seconds() // 60))
    relv = compute_rel_vol(v, adv10, window_minutes)

    ref = polygon_reference(ticker, log_fn=log_fn) or {}
    is_cs = (ref.get("type") == "CS" and ref.get("active") is True)
    if not is_cs:
        return None

    float_shares = None
    for k in ("share_class_shares_outstanding", "weighted_shares_outstanding", "shares_outstanding"):
        vv = safe_int(ref.get(k))
        if vv and vv > 0:
            float_shares = vv
            break

    trigger = h
    stop = l

    return {
        "ticker": ticker,
        "open": o, "high": h, "low": l, "close": c,
        "vol": v, "dollar_vol": dollar_vol,
        "range_pct": range_pct,
        "hold_pct": hold_pct,
        "move_pct": move_pct,
        "rel_vol": relv,
        "float_shares": float_shares,
        "trigger": trigger,
        "stop": stop,
    }


# ============================================================
# Scoring + confidence
# ============================================================
def compute_scores(feat: dict) -> tuple[float, float, float]:
    """
    Returns (base_score, prob, pressure_score)
    pressure_score is used to build "SQUEEZE WATCH" when no true squeezes exist.
    """
    si = feat.get("si_pct_ff") or 0.0
    si_chg = feat.get("si_pct_chg") or 0.0
    ctb = feat.get("ctb") or 0.0
    avail = feat.get("avail")

    avail_term = 0.0
    if avail is not None:
        avail_term = max(0.0, 6.0 - math.log10(max(avail, 1.0)))

    pressure = (si * 2.0) + (si_chg * 8.0) + (min(ctb, 200.0) * 0.10) + (avail_term * 2.0)

    rng = feat.get("range_pct") or 0.0
    move = abs(feat.get("move_pct") or 0.0)
    relv = feat.get("rel_vol") or 0.0
    dv = (feat.get("dollar_vol") or 0.0) / 1_000_000.0
    opportunity = (rng * 120.0) + (move * 35.0) + (min(relv, 6.0) * 10.0) + (min(dv, 15.0) * 4.0)

    hold = feat.get("hold_pct") or 0.0
    structure = (hold * 70.0) + (clamp((0.12 - abs(rng - 0.06)) / 0.12) * 30.0)

       # ---------------- PREMARKET EDGE ADJUSTMENT ----------------
    # Premarket = more about pressure, less about volume confirmation

    window = feat.get("window")  # we’ll pass this in shortly

    if window == "PREMARKET":
        pressure_weight = 0.38     # +10% pressure bias
        opportunity_weight = 0.30  # slightly lower
        structure_weight = 0.32
    else:
        pressure_weight = 0.34
        opportunity_weight = 0.33
        structure_weight = 0.33

    base_score = (
        pressure_weight * pressure +
        opportunity_weight * opportunity +
        structure_weight * structure
    )

    prob = score_to_prob(base_score)
    return base_score, prob, pressure


def confidence_1_to_10(prob: float, ortex_on: bool, has_borrow: bool) -> int:
    p = prob
    if not ortex_on:
        p = p * 0.86

    if ortex_on and has_borrow and p >= 0.965:
        return 10

    c = int(round(clamp(p, 0, 0.99) * 8)) + 1
    if not ortex_on and c >= 9:
        c = 8 if p < 0.93 else 9
    return max(1, min(9, c))

def is_true_squeeze(feat: dict, ortex_on: bool) -> bool:
    if not ortex_on:
        return False

    si = feat.get("si_pct_ff")
    if si is None or si < 8.0:
        return False

    if feat.get("ctb") is None or feat.get("avail") is None:
        return False

    if feat.get("avail") is not None and feat["avail"] > 250_000:
        return False

    if (feat.get("dollar_vol") or 0.0) < 250_000:
        return False

    if (feat.get("hold_pct") or 0.0) < 0.25:
        return False

    if (feat.get("range_pct") or 0.0) < 0.02:
        return False

    return True

def setup_subtype(feat: dict, label: str) -> str:
    # label in {"TRUE_SQUEEZE","SQUEEZE_WATCH","MOMENTUM"}
    if label == "TRUE_SQUEEZE":
        return "true squeeze breakout" if (feat.get("hold_pct") or 0) >= 0.30 else "true squeeze pullback"

    if label == "SQUEEZE_WATCH":
        return "squeeze watch"

    if (feat.get("dollar_vol") or 0) < 300_000:
        return "thin momentum"

    if (feat.get("hold_pct") or 0) >= 0.35 and 0.02 <= (feat.get("range_pct") or 0) <= 0.12:
        return "momentum breakout"

    return "momentum pullback"

def trade_plan(feat: dict) -> str:
    trig = feat.get("trigger") or 0.0
    stop = feat.get("stop") or 0.0

    risk = max(trig - stop, 0.0001)

    target1 = trig + (risk * 2.0)   # disciplined target
    ceiling = trig + (risk * 3.0)   # anti-greed ceiling

    return (
        f"ENTRY {trig:.4f} | "
        f"STOP {stop:.4f} | "
        f"TARGET {target1:.4f} (2R) | "
        f"CEILING {ceiling:.4f} (AUTO-SELL ZONE). "
        f"System rule: Sell majority at TARGET. "
        f"If CEILING hits, exit fully — no exceptions."
    )



# ============================================================
# HTML report
# ============================================================
def write_html_report(meta: dict, squeeze_rows: list[dict], momentum_rows: list[dict]) -> str:
    ts = now_ct().strftime("%Y-%m-%d_%H-%M-%S")
    base = f"scan_{meta['date']}_{ts}"
    html_path = OUT_DIR / f"{base}.html"

    def fmt(x, nd=2):
        if x is None:
            return "NA"
        try:
            return f"{float(x):.{nd}f}"
        except Exception:
            return "NA"

    def pct(x, nd=1):
        return fmt((x or 0) * 100.0, nd) + "%"

    def row_tr(r):
        return f"""
        <tr>
          <td><b>{r["ticker"]}</b></td>
          <td>{r["confidence"]}</td>
          <td>{r.get("subtype","")}</td>
          <td>{fmt(r.get("close"),2)}</td>
          <td>{pct(r.get("move_pct"),1)}</td>
          <td>{fmt(r.get("dollar_vol"),0)}</td>
          <td>{pct(r.get("range_pct"),1)}</td>
          <td>{fmt(r.get("si_pct_ff"),1)}</td>
          <td>{fmt(r.get("ctb"),1)}</td>
          <td>{fmt(r.get("avail"),0)}</td>
        </tr>
        """

    def table(rows):
        if not rows:
            return "<div class='muted'>None</div>"
        body = "".join(row_tr(r) for r in rows)
        return f"""
        <table>
          <thead>
            <tr>
              <th>Ticker</th><th>Conf</th><th>Subtype</th><th>Price</th><th>Move</th><th>$Vol</th><th>Range</th>
              <th>SI%</th><th>CTB</th><th>Avail</th>
            </tr>
          </thead>
          <tbody>{body}</tbody>
        </table>
        """

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>SqueezeBot {meta['date']}</title>
<style>
body {{ font-family: Arial, sans-serif; background:#0b0f14; color:#e7eefc; margin:24px; }}
.card {{ background:#111827; border:1px solid #253045; border-radius:16px; padding:16px; margin-bottom:16px; }}
h1 {{ margin:0 0 6px 0; }}
.muted {{ color:#93a4bd; }}
table {{ width:100%; border-collapse:collapse; }}
th,td {{ border-bottom:1px solid #253045; padding:10px; font-size:14px; vertical-align:top; }}
th {{ text-align:left; color:#93a4bd; font-weight:700; }}
</style></head>
<body>
<div class="card">
  <h1>SqueezeBot</h1>
  <div class="muted">{meta.get("date")} • Window: {meta.get("window")} • Mode: {meta.get("mode")} • ORTEX: {meta.get("ortex")}</div>
</div>
<div class="card">
  <h2>SQUEEZE</h2>
  <div class="muted">True squeezes (confirmed) + Squeeze Watch (if none confirmed)</div>
  {table(squeeze_rows)}
</div>
<div class="card">
  <h2>MOMENTUM</h2>
  <div class="muted">Gainers only</div>
  {table(momentum_rows)}
</div>
</body></html>
"""
    html_path.write_text(html, encoding="utf-8")
    return str(html_path).replace("\\", "/")


# ============================================================
# Public entrypoint
# ============================================================
def run_scan(log_fn=None, row_fn=None, mode: str = "day", ortex: str = "auto") -> str | None:
    mode = (mode or "day").lower().strip()
    if mode not in ("day", "night"):
        mode = "day"

    ortex = (ortex or "auto").lower().strip()
    if ortex not in ("auto", "on", "off"):
        ortex = "auto"

    dt = now_ct()
    window, w_start, w_end = current_market_window(dt)
    date_str = ct_date_str(w_start)

    # Decide ORTEX actual on/off
    if ortex == "off":
        ortex_on = False
    elif ortex == "on":
        # force ON if key exists (still respects coverage limits naturally via data availability)
        ortex_on = bool(ORTEX_KEY)
    else:
        # auto behavior
        ortex_on = (mode == "day") and ortex_allowed_now(dt) and bool(ORTEX_KEY)

    deep_top = DEEP_ANALYZE_TOP_DAY if mode == "day" else DEEP_ANALYZE_TOP_NIGHT
    ortex_finalists = ORTEX_FINALISTS_DAY if ortex_on else ORTEX_FINALISTS_NIGHT

    if log_fn:
        log_fn(f"Mode: {mode.upper()} ({'Polygon + ORTEX' if (mode=='day') else 'Polygon only'})")
        log_fn(f"Date: {date_str}")
        log_fn(f"Market window: {window} ({w_start.strftime('%H:%M')}–{w_end.strftime('%H:%M')} CT)")
        log_fn(f"ORTEX request: {ortex.upper()} | ORTEX mode: {'ON' if ortex_on else 'OFF'} (ON only 7:30AM–4:00PM CT)")

    snap = get_snapshot_all_tickers(log_fn=log_fn)
    if log_fn:
        log_fn(f"Snapshot tickers received: {len(snap)}")

    candidates: list[tuple[float, str, dict]] = []
    for pmin, pmax in PRICE_TIERS:
        candidates.extend(pick_snapshot_candidates(snap, pmin, pmax))
    candidates.sort(reverse=True, key=lambda x: x[0])

    deep_list = candidates[:deep_top]
    if log_fn:
        log_fn(f"Deep-analyze list: {len(deep_list)} tickers (Top {deep_top})")

    analyzed: list[dict] = []

    # PASS 1: Polygon deep analysis for all deep_list
    for idx, (_proxy_score, ticker, snap_meta) in enumerate(deep_list, start=1):
        if log_fn and idx % 10 == 0:
            log_fn(f"Analyzing (Polygon) {idx}/{len(deep_list)}...")

        try:
            feat = analyze_ticker_window(ticker, date_str, w_start, w_end, snap_meta, log_fn=log_fn)
            if not feat:
                continue

            feat["window"] = window

            feat["si_pct_ff"] = None
            feat["si_pct_chg"] = None
            feat["ctb"] = None
            feat["avail"] = None

            base_score, prob, pressure = compute_scores(feat)
            conf = confidence_1_to_10(prob, ortex_on=False, has_borrow=False)

            row = {
                "ticker": ticker,
                "bucket": "MOMENTUM",
                "subtype": setup_subtype(feat, label="MOMENTUM"),
                "confidence": conf,
                "prob": round(prob, 4),
                "base_score": round(base_score, 2),
                "pressure_score": round(pressure, 2),

                "close": feat.get("close"),
                "move_pct": feat.get("move_pct"),
                "dollar_vol": feat.get("dollar_vol"),
                "range_pct": feat.get("range_pct"),
                "hold_pct": feat.get("hold_pct"),
                "rel_vol": feat.get("rel_vol"),
                "float_shares": feat.get("float_shares"),

                "si_pct_ff": None,
                "ctb": None,
                "avail": None,

                "trigger": feat.get("trigger"),
                "stop": feat.get("stop"),
                "plan": trade_plan(feat),
            }

            analyzed.append(row)

        except Exception:
            continue

    if not analyzed:
        if log_fn:
            log_fn("No tickers passed deep analysis.")
        return None

    # Pick ORTEX finalists (only if ortex_on)
    finalists = []
    if ortex_on and ortex_finalists > 0:
        finalists = sorted(
            analyzed,
            key=lambda r: (
                r.get("base_score", 0),
                r.get("pressure_score", 0),
                (r.get("hold_pct") or 0),
                (r.get("range_pct") or 0),
                (r.get("dollar_vol") or 0),
            ),
            reverse=True
        )[:ortex_finalists]
        if log_fn:
            log_fn(f"ORTEX enrich: {len(finalists)} finalists selected.")

    # PASS 2: ORTEX enrich finalists + recompute score/conf + maybe upgrade to SQUEEZE
    for r in finalists:
        t = r["ticker"]
        try:
            si = ortex_short_interest_features(t, log_fn=log_fn) or {}
            r["si_pct_ff"] = si.get("si_pct_ff")
            r["si_pct_chg"] = si.get("si_pct_chg")
            r["ctb"] = ortex_ctb_latest(t, log_fn=log_fn)
            r["avail"] = ortex_availability_latest(t, log_fn=log_fn)

            feat = dict(r)
            base_score, prob, pressure = compute_scores(feat)
            has_borrow = (r.get("ctb") is not None and r.get("avail") is not None)
            r["base_score"] = round(base_score, 2)
            r["prob"] = round(prob, 4)
            r["pressure_score"] = round(pressure, 2)
            r["confidence"] = confidence_1_to_10(prob, ortex_on=True, has_borrow=has_borrow)

            if is_true_squeeze(feat, ortex_on=True):
                r["bucket"] = "SQUEEZE"
                r["subtype"] = setup_subtype(feat, label="TRUE_SQUEEZE")
            else:
                r["bucket"] = "MOMENTUM"
                r["subtype"] = setup_subtype(feat, label="MOMENTUM")

            r["plan"] = trade_plan(feat)

        except Exception:
            continue

    # Build final squeeze list
    squeezes_true = [r for r in analyzed if r.get("bucket") == "SQUEEZE"]

    if squeezes_true:
        squeezes = sorted(
            squeezes_true,
            key=lambda r: (r.get("confidence", 0), r.get("base_score", 0)),
            reverse=True
        )[:TOP_N_PER_BUCKET]
    else:
        # SQUEEZE WATCH (always fill tab)
        watch_sorted = sorted(
            analyzed,
            key=lambda r: (r.get("pressure_score", 0), r.get("base_score", 0), (r.get("dollar_vol") or 0)),
            reverse=True
        )
        squeezes = []
        for r in watch_sorted:
            rr = dict(r)
            rr["bucket"] = "SQUEEZE"
            rr["subtype"] = "squeeze watch"
            rr["confidence"] = min(int(rr.get("confidence") or 1), 7)
            rr["plan"] = (rr.get("plan") or "") + " (Watchlist: ORTEX confirmation not present.)"
            squeezes.append(rr)
            if len(squeezes) >= TOP_N_PER_BUCKET:
                break

    # Build final momentum list (gainers only)
    momentum = [
        r for r in analyzed
        if r.get("bucket") != "SQUEEZE"
        and (r.get("move_pct") is not None)
        and (r["move_pct"] > 0)
    ]
    momentum = sorted(
        momentum,
        key=lambda r: (r.get("confidence", 0), r.get("base_score", 0)),
        reverse=True
    )[:TOP_N_PER_BUCKET]

    # ✅ Stream ONLY final top rows to website
    if row_fn:
        for r in squeezes + momentum:
            row_fn(r)

    meta = {
        "date": date_str,
        "window": window,
        "mode": mode.upper(),
        "ortex": "ON" if ortex_on else "OFF",
    }

    html_path = write_html_report(meta, squeezes, momentum)

    if log_fn:
        log_fn(f"Saved HTML: {html_path}")

    return html_path
