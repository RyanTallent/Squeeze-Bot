# scanner.py
import os
import csv
import json
import math
import time
import requests
from pathlib import Path
from datetime import datetime, timedelta, timezone

# ============================================================
# ENGINE V2 — SIMPLE, RELIABLE, STREAMING-FRIENDLY
# ============================================================

# Output sizing
TOP_N_PER_BUCKET = 5         # saved HTML report shows top N per bucket
DEEP_ANALYZE_TOP = 75         # you wanted top 75 so we don't miss stuff
ORTEX_FINALISTS = 25          # ORTEX only for the first 25 to save credits

# Moderate snapshot filter (cheap “whole market” pass)
MIN_DOLLAR_VOL_PROXY = 250_000
MIN_MOVE_PROXY_PCT = 0.01

# Price tiers
PRICE_TIERS = [(0.01, 2.50), (2.50, 5.00), (5.00, 10.00)]
MAX_CANDIDATES_PER_TIER_SNAPSHOT = 600  # cheap list before deep analysis

# Market hours (Central Time)
PM_START_CT = (3, 0)
PM_END_CT   = (8, 29)

REG_START_CT = (8, 30)
REG_END_CT   = (15, 0)

AH_START_CT = (15, 0)
AH_END_CT   = (19, 0)

# ORTEX hours (your preference)
ORTEX_ON_START_CT = (7, 30)
ORTEX_ON_END_CT   = (16, 0)

# Keys
POLYGON_KEY = os.getenv("POLYGON_API_KEY")
ORTEX_KEY = os.getenv("ORTEX_API_KEY")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "squeeze-bot/engine-v2"})

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
# Market window logic (CURRENT window; CLOSED => last AFTERHOURS)
# ============================================================
def current_market_window(dt: datetime) -> tuple[str, datetime, datetime]:
    """
    Returns (window_name, start_local, end_local) in CT.
    If market is CLOSED, returns the most recent AFTERHOURS session (15:00–19:00 CT).
    """
    # weekends: treat as CLOSED and fallback to last weekday AH
    if dt.weekday() >= 5:
        # go back to Friday
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

    # CLOSED fallback to most recent after-hours session
    if dt > ah_end:
        # after 7pm → use today's AH full session
        return ("AFTERHOURS (LAST)", ah_start, ah_end)

    # before 3am (but weekday) → use yesterday's AH
    y = dt - timedelta(days=1)
    # if yesterday is weekend, fallback to Friday
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
# Polygon (throttle-safe + logs)
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
# ORTEX (time-gated)
# ============================================================
def ortex_get(url: str, log_fn=None) -> dict | None:
    if not ORTEX_KEY:
        return None

    r = SESSION.get(url, headers={"Ortex-Api-Key": ORTEX_KEY}, timeout=30)

    if r.status_code == 404:
        if log_fn:
            log_fn(f"[ORTEX 404] no coverage {url}")
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
# Candidate selection (cheap whole-market pass)
# ============================================================
def pick_snapshot_candidates(snap: list[dict], price_min: float, price_max: float) -> list[tuple[float, str, dict]]:
    out = []
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

        # moderate cheap filter
        if dollar_vol_proxy < MIN_DOLLAR_VOL_PROXY and abs(move_pct) < MIN_MOVE_PROXY_PCT:
            continue

        proxy = (dollar_vol_proxy / 1_000_000.0) + (abs(move_pct) * 6.0)
        out.append((proxy, t, {"last": p, "prev_close": prev_close, "day_vol": day_vol}))

    out.sort(reverse=True, key=lambda x: x[0])
    return out[:MAX_CANDIDATES_PER_TIER_SNAPSHOT]


# ============================================================
# Deep analysis for the chosen market window
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
    rr = (rng / max(trigger - stop, 1e-9))

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
        "rr": rr,
    }


# ============================================================
# Scoring + confidence 1–10 (10 rare, polygon-only stricter)
# ============================================================
def compute_scores(feat: dict) -> tuple[float, float]:
    # pressure (ORTEX helps)
    si = feat.get("si_pct_ff") or 0.0
    si_chg = feat.get("si_pct_chg") or 0.0
    ctb = feat.get("ctb") or 0.0
    avail = feat.get("avail")

    avail_term = 0.0
    if avail is not None:
        avail_term = max(0.0, 6.0 - math.log10(max(avail, 1.0)))

    pressure = (si * 2.0) + (si_chg * 8.0) + (min(ctb, 200.0) * 0.10) + (avail_term * 2.0)

    # opportunity
    rng = feat.get("range_pct") or 0.0
    move = abs(feat.get("move_pct") or 0.0)
    relv = feat.get("rel_vol") or 0.0
    dv = (feat.get("dollar_vol") or 0.0) / 1_000_000.0
    opportunity = (rng * 120.0) + (move * 35.0) + (min(relv, 6.0) * 10.0) + (min(dv, 15.0) * 4.0)

    # structure
    hold = feat.get("hold_pct") or 0.0
    rr = feat.get("rr") or 0.0
    structure = (hold * 60.0) + (clamp(rr / 5.0) * 30.0) + (clamp((0.12 - abs(rng - 0.06)) / 0.12) * 10.0)

    base_score = 0.34 * pressure + 0.33 * opportunity + 0.33 * structure
    prob = score_to_prob(base_score)
    return base_score, prob


def confidence_1_to_10(prob: float, ortex_on: bool, has_borrow: bool) -> int:
    p = prob
    if not ortex_on:
        p = p * 0.86  # stricter without ORTEX

    # 10 is very rare: requires ORTEX + borrow confirmation + extremely high p
    if ortex_on and has_borrow and p >= 0.965:
        return 10

    # map into 1..9
    c = int(round(clamp(p, 0, 0.99) * 8)) + 1

    # polygon-only should rarely hit 9
    if not ortex_on and c >= 9:
        c = 8 if p < 0.93 else 9

    return max(1, min(9, c))


def is_true_squeeze(feat: dict, ortex_on: bool) -> bool:
    # Only label SQUEEZE if ORTEX is ON and borrow data is present
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


def setup_subtype(feat: dict, squeeze: bool) -> str:
    if squeeze:
        return "squeeze breakout" if (feat.get("hold_pct") or 0) >= 0.30 else "squeeze pullback"

    if (feat.get("dollar_vol") or 0) < 300_000:
        return "thin momentum"

    if (feat.get("hold_pct") or 0) >= 0.35 and 0.02 <= (feat.get("range_pct") or 0) <= 0.12:
        return "momentum breakout"

    return "momentum pullback"


def trade_plan(feat: dict) -> str:
    trig = feat.get("trigger") or 0.0
    stop = feat.get("stop") or 0.0
    return f"Entry near trigger {trig:.4f}. Stop {stop:.4f}. Avoid chasing >2% above trigger."


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
  <div class="muted">{meta.get("date")} • Window: {meta.get("window")} • ORTEX: {meta.get("ortex")}</div>
</div>
<div class="card">
  <h2>SQUEEZE</h2>
  <div class="muted">Ranked by Confidence 1–10</div>
  {table(squeeze_rows)}
</div>
<div class="card">
  <h2>MOMENTUM</h2>
  <div class="muted">Ranked by Confidence 1–10</div>
  {table(momentum_rows)}
</div>
</body></html>
"""
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    return str(html_path).replace("\\", "/")


# ============================================================
# Public entrypoint (called by main.py)
# ============================================================
def run_scan(log_fn=None, row_fn=None) -> str | None:
    dt = now_ct()
    window, w_start, w_end = current_market_window(dt)

    # IMPORTANT: date_str must match the window date (yesterday fallback)
    date_str = ct_date_str(w_start)

    ortex_on = ortex_allowed_now(dt)

    if log_fn:
        log_fn(f"Date: {date_str}")
        log_fn(f"Market window: {window} ({w_start.strftime('%H:%M')}–{w_end.strftime('%H:%M')} CT)")
        log_fn(f"ORTEX mode: {'ON' if ortex_on else 'OFF'} (ON only 7:30AM–4:00PM CT)")

    snap = get_snapshot_all_tickers(log_fn=log_fn)
    if log_fn:
        log_fn(f"Snapshot tickers received: {len(snap)}")

    # Step 1: cheap whole-market scan -> ranked candidates
    candidates: list[tuple[float, str, dict]] = []
    for pmin, pmax in PRICE_TIERS:
        candidates.extend(pick_snapshot_candidates(snap, pmin, pmax))

    candidates.sort(reverse=True, key=lambda x: x[0])

    deep_list = candidates[:DEEP_ANALYZE_TOP]
    if log_fn:
        log_fn(f"Deep-analyze list: {len(deep_list)} tickers (Top {DEEP_ANALYZE_TOP})")

    analyzed: list[dict] = []

    for idx, (proxy_score, ticker, snap_meta) in enumerate(deep_list, start=1):
        if log_fn and idx % 10 == 0:
            log_fn(f"Analyzing {idx}/{len(deep_list)}...")

        try:
            feat = analyze_ticker_window(ticker, date_str, w_start, w_end, snap_meta, log_fn=log_fn)
            if not feat:
                continue

            # ORTEX enrich only for top 25 (and only if allowed)
            if ortex_on and idx <= ORTEX_FINALISTS:
                si = ortex_short_interest_features(ticker, log_fn=log_fn) or {}
                feat["si_pct_ff"] = si.get("si_pct_ff")
                feat["si_pct_chg"] = si.get("si_pct_chg")
                feat["ctb"] = ortex_ctb_latest(ticker, log_fn=log_fn)
                feat["avail"] = ortex_availability_latest(ticker, log_fn=log_fn)
            else:
                feat["si_pct_ff"] = None
                feat["si_pct_chg"] = None
                feat["ctb"] = None
                feat["avail"] = None

            squeeze = is_true_squeeze(feat, ortex_on=ortex_on)

            base_score, prob = compute_scores(feat)
            has_borrow = (feat.get("ctb") is not None and feat.get("avail") is not None)
            conf = confidence_1_to_10(prob, ortex_on=ortex_on, has_borrow=has_borrow)

            bucket = "SQUEEZE" if squeeze else "MOMENTUM"
            subtype = setup_subtype(feat, squeeze=squeeze)
            plan = trade_plan(feat)

            row = {
                "ticker": ticker,
                "bucket": bucket,
                "subtype": subtype,
                "confidence": conf,
                "prob": round(prob, 4),
                "base_score": round(base_score, 2),
                "close": feat.get("close"),
                "move_pct": feat.get("move_pct"),
                "dollar_vol": feat.get("dollar_vol"),
                "range_pct": feat.get("range_pct"),
                "hold_pct": feat.get("hold_pct"),
                "rel_vol": feat.get("rel_vol"),
                "float_shares": feat.get("float_shares"),
                "si_pct_ff": feat.get("si_pct_ff"),
                "ctb": feat.get("ctb"),
                "avail": feat.get("avail"),
                "trigger": feat.get("trigger"),
                "stop": feat.get("stop"),
                "plan": plan,
            }

            analyzed.append(row)

            # Stream row to UI immediately
            if row_fn:
                row_fn(row)

        except Exception:
            continue

    if not analyzed and log_fn:
        log_fn("No deep-analysis rows produced. (Possible Polygon throttling or empty window data)")

    squeezes = [r for r in analyzed if r["bucket"] == "SQUEEZE"]
    momentum = [r for r in analyzed if r["bucket"] == "MOMENTUM"]

    squeezes.sort(key=lambda r: (r["confidence"], r.get("base_score", 0)), reverse=True)
    momentum.sort(key=lambda r: (r["confidence"], r.get("base_score", 0)), reverse=True)

    meta = {"date": date_str, "window": window, "ortex": "ON" if ortex_on else "OFF"}
    html_path = write_html_report(meta, squeezes[:TOP_N_PER_BUCKET], momentum[:TOP_N_PER_BUCKET])

    if log_fn:
        log_fn(f"Saved HTML: {html_path}")

    return html_path
