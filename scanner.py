import os
import json
import math
import time
import requests
from pathlib import Path
from datetime import datetime, timedelta, timezone

# ============================================================
# ENGINE V2.4+ — RYAN FILTERS + VWAP PULLBACK LOGIC
# Keeps: ORTEX toggle, market windows, tiered snapshot, deep analysis,
# streams only final top 5, writes HTML report.
# ============================================================

TOP_N_PER_BUCKET = 5

DEEP_ANALYZE_TOP_DAY = 75
ORTEX_FINALISTS_DAY = 25

DEEP_ANALYZE_TOP_NIGHT = 35
ORTEX_FINALISTS_NIGHT = 0

MIN_DOLLAR_VOL_PROXY = 250_000
MIN_MOVE_PROXY_PCT = 0.01

PRICE_TIERS = [(0.01, 2.50), (2.50, 5.00), (5.00, 10.00)]
MAX_CANDIDATES_PER_TIER_SNAPSHOT = 600

# ---- Market windows (CT) ----
PM_START_CT = (3, 0)
PM_END_CT   = (8, 29)

REG_START_CT = (8, 30)
REG_END_CT   = (15, 0)

AH_START_CT = (15, 0)
AH_END_CT   = (19, 0)

ORTEX_ON_START_CT = (7, 0)
ORTEX_ON_END_CT   = (16, 0)

POLYGON_KEY = os.getenv("POLYGON_API_KEY")
ORTEX_KEY = os.getenv("ORTEX_API_KEY")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "squeeze-bot/engine-v2.4-ryan"})

OUT_DIR = Path("outputs")
OUT_DIR.mkdir(exist_ok=True)

# ============================================================
# RYAN FILTERS (your rules)
# ============================================================
MAX_MARKET_CAP = 2_000_000_000      # 2B
MAX_FLOAT_SHARES = 50_000_000       # 50M
MIN_REL_VOL = 1.5
MIN_WINDOW_VOL = 5_000_000          # 5M volume
MIN_RANGE_PCT = 0.03                # want range; hard floor 3%
# "near VWAP" threshold (2% from vwap)
VWAP_NEAR_PCT = 0.02

try:
    from zoneinfo import ZoneInfo
    CT_TZ = ZoneInfo("America/Chicago")
except Exception:
    CT_TZ = timezone(timedelta(hours=-6))


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
# Market Window
# ============================================================
def current_market_window(dt: datetime) -> tuple[str, datetime, datetime]:
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

def resolve_ortex_on(mode: str, ortex_request: str, dt: datetime) -> tuple[bool, str]:
    if not ORTEX_KEY:
        return (False, "OFF")

    req = (ortex_request or "off").strip().lower()
    if req not in ("on", "off"):
        req = "off"

    if req == "off":
        return (False, "OFF")

    if ortex_allowed_now(dt):
        return (True, "ON")
    return (False, "OFF (outside 7am–4pm CT)")


# ============================================================
# Polygon / Ortex fetchers
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
# VWAP + Pullback helpers
# ============================================================
def calc_vwap(minute_bars: list[dict]) -> float | None:
    pv = 0.0
    vv = 0.0
    for b in minute_bars:
        try:
            tp = (float(b["h"]) + float(b["l"]) + float(b["c"])) / 3.0
            v = float(b["v"])
            pv += tp * v
            vv += v
        except Exception:
            continue
    if vv <= 0:
        return None
    return pv / vv

def vwap_pullback_score(close: float, vwap: float | None, high: float, low: float) -> float:
    """
    Approximate 'break then pullback to VWAP':
    - price still strong (close in upper half of range)
    - close near vwap (within ~2%)
    - close above vwap (optional bonus)
    """
    if vwap is None or close <= 0:
        return 0.0

    rng = max(high - low, 1e-9)
    hold = (close - low) / rng  # 0..1 (upper range strength)
    near = abs(close - vwap) / close  # fraction

    score = 0.0
    if hold >= 0.55:
        score += 8.0
    if near <= VWAP_NEAR_PCT:
        score += 10.0
    if close >= vwap:
        score += 6.0
    return score


# ============================================================
# Snapshot candidate selection (same as you had)
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
        out.append((proxy, t, {"prev_close": prev_close, "last_price": p}))

    out.sort(reverse=True, key=lambda x: x[0])
    return out[:MAX_CANDIDATES_PER_TIER_SNAPSHOT]


def compute_rel_vol(window_vol: float, avg_daily_vol: float | None, window_minutes: int) -> float | None:
    if avg_daily_vol is None or avg_daily_vol <= 0:
        return None
    expected = avg_daily_vol * (window_minutes / 390.0)
    if expected <= 0:
        return None
    return window_vol / expected


# ============================================================
# Deep analysis per ticker (THIS is where your new filters go)
# ============================================================
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
    if len(window) < 10:
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

    # ---- Ryan hard filters: volume + range ----
    if v < MIN_WINDOW_VOL:
        return None
    if range_pct < MIN_RANGE_PCT:
        return None

    daily = get_daily_aggs_20d(ticker, date_str, log_fn=log_fn)
    vols = [safe_float(b.get("v")) for b in daily[-10:]] if daily else []
    vols = [x for x in vols if x is not None]
    adv10 = (sum(vols) / len(vols)) if vols else None

    window_minutes = max(1, int((w_end - w_start).total_seconds() // 60))
    relv = compute_rel_vol(v, adv10, window_minutes)

    # Ryan hard filter: rel vol
    if relv is None or relv < MIN_REL_VOL:
        return None

    ref = polygon_reference(ticker, log_fn=log_fn) or {}
    is_cs = (ref.get("type") == "CS" and ref.get("active") is True)
    if not is_cs:
        return None

    # Float shares proxy
    float_shares = None
    for k in ("share_class_shares_outstanding", "weighted_shares_outstanding", "shares_outstanding"):
        vv = safe_int(ref.get(k))
        if vv and vv > 0:
            float_shares = vv
            break

    # Ryan hard filter: float
    if float_shares is None or float_shares > MAX_FLOAT_SHARES:
        return None

    # Market cap estimate = last price * shares_outstanding proxy
    last_price = snap_meta.get("last_price") or c
    try:
        market_cap = float(last_price) * float(float_shares)
    except Exception:
        market_cap = None

    if market_cap is None or market_cap > MAX_MARKET_CAP:
        return None

    # VWAP from window bars
    vwap = calc_vwap(window)

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
        "market_cap": market_cap,
        "vwap": vwap,
        "trigger": trigger,
        "stop": stop,
    }


# ============================================================
# Scoring (keeps yours, adds your pullback preferences)
# ============================================================
def compute_scores(feat: dict) -> tuple[float, float, float]:
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

    # Base opportunity (yours)
    opportunity = (rng * 120.0) + (move * 35.0) + (min(relv, 6.0) * 10.0) + (min(dv, 15.0) * 4.0)

    # Ryan preference: "lots of volume and lots of range" => extra boost
    # - range bonus scales after MIN_RANGE_PCT
    # - volume bonus scales after MIN_WINDOW_VOL
    range_bonus = max(0.0, (rng - MIN_RANGE_PCT)) * 220.0
    vol_bonus = max(0.0, ((feat.get("vol") or 0.0) - MIN_WINDOW_VOL) / 1_000_000.0) * 1.6

    # Pullback to VWAP boost
    vwap_bonus = vwap_pullback_score(
        close=float(feat.get("close") or 0.0),
        vwap=feat.get("vwap"),
        high=float(feat.get("high") or 0.0),
        low=float(feat.get("low") or 0.0),
    )

    opportunity = opportunity + range_bonus + vol_bonus + vwap_bonus

    hold = feat.get("hold_pct") or 0.0
    structure = (hold * 70.0) + (clamp((0.12 - abs(rng - 0.06)) / 0.12) * 30.0)

    window = feat.get("window")
    if window == "PREMARKET":
        pressure_weight = 0.38
        opportunity_weight = 0.30
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
    # Minor tweak: if close near VWAP, call it pullback
    c = feat.get("close") or 0.0
    vwap = feat.get("vwap")
    near_vwap = False
    if vwap and c:
        near_vwap = (abs(c - vwap) / c) <= VWAP_NEAR_PCT

    if label == "TRUE_SQUEEZE":
        return "true squeeze breakout" if (feat.get("hold_pct") or 0) >= 0.30 else "true squeeze pullback"
    if label == "SQUEEZE_WATCH":
        return "squeeze watch"
    if (feat.get("dollar_vol") or 0) < 300_000:
        return "thin momentum"
    if near_vwap:
        return "VWAP pullback"
    if (feat.get("hold_pct") or 0) >= 0.35 and 0.02 <= (feat.get("range_pct") or 0) <= 0.12:
        return "momentum breakout"
    return "momentum pullback"


def trade_plan(feat: dict) -> str:
    trig = feat.get("trigger") or 0.0
    stop = feat.get("stop") or 0.0
    vwap = feat.get("vwap")
    risk = max(trig - stop, 0.0001)
    target1 = trig + (risk * 2.0)
    ceiling = trig + (risk * 3.0)

    vwap_note = ""
    if vwap is not None:
        vwap_note = f" VWAP {vwap:.4f} (ideal pullback zone)."

    return (
        f"ENTRY {trig:.4f} | "
        f"STOP {stop:.4f} | "
        f"TARGET {target1:.4f} (2R) | "
        f"CEILING {ceiling:.4f} (AUTO-SELL ZONE)."
        f"{vwap_note} "
        f"Beginner note: Trigger = breakout high, Stop = session low. "
        f"System rule: Sell majority at TARGET. "
        f"If CEILING hits, exit fully — no exceptions."
    )


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
          <td>{fmt(r.get("rel_vol"),2)}</td>
          <td>{fmt(r.get("float_shares"),0)}</td>
          <td>{fmt(r.get("market_cap"),0)}</td>
          <td>{fmt(r.get("vwap"),4)}</td>
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
              <th>RelVol</th><th>Float</th><th>MktCap</th><th>VWAP</th>
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
  <h1>Precipice Analytica</h1>
  <div class="muted">by Ryan Tallent • {meta.get("date")} • Window: {meta.get("window")} • Mode: {meta.get("mode")} • ORTEX: {meta.get("ortex")}</div>
  <div class="muted">Filters: MktCap&lt;2B • Float&lt;50M • Vol&gt;5M • RelVol&gt;1.5 • Range&gt;3% • VWAP pullback bonus</div>
</div>
<div class="card">
  <h2>SQUEEZE</h2>
  <div class="muted">True squeezes (confirmed) + Squeeze Watch (if none confirmed)</div>
  {table(squeeze_rows)}
</div>
<div class="card">
  <h2>MOMENTUM</h2>
  <div class="muted">Gainers only (Ryan filters applied)</div>
  {table(momentum_rows)}
</div>
</body></html>
"""
    html_path.write_text(html, encoding="utf-8")
    return str(html_path).replace("\\", "/")


# ============================================================
# Main run_scan (keeps your pipeline)
# ============================================================
def run_scan(log_fn=None, row_fn=None, mode: str = "day", ortex: str = "off") -> str | None:
    mode = (mode or "day").lower().strip()
    if mode not in ("day", "night"):
        mode = "day"

    dt = now_ct()
    window, w_start, w_end = current_market_window(dt)
    date_str = ct_date_str(w_start)

    ortex_on, ortex_label = resolve_ortex_on(mode, ortex, dt)

    deep_top = DEEP_ANALYZE_TOP_DAY if mode == "day" else DEEP_ANALYZE_TOP_NIGHT
    ortex_finalists = ORTEX_FINALISTS_DAY if ortex_on else ORTEX_FINALISTS_NIGHT

    if log_fn:
        log_fn(f"Mode: {mode.upper()} ({'Polygon + ORTEX' if ortex_on else 'Polygon only'})")
        log_fn(f"Date: {date_str}")
        log_fn(f"Market window: {window} ({w_start.strftime('%H:%M')}–{w_end.strftime('%H:%M')} CT)")
        log_fn(f"ORTEX request: {str(ortex).upper()} | ORTEX mode: {ortex_label}")
        log_fn(f"Ryan filters → MktCap<2B | Float<50M | Vol>5M | RelVol>1.5 | Range>3% | VWAP pullback bonus")

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
                "market_cap": feat.get("market_cap"),
                "vwap": feat.get("vwap"),
                "vol": feat.get("vol"),

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

    for r in finalists:
        t = r["ticker"]
        try:
            si = ortex_short_interest_features(t, log_fn=log_fn) or {}
            r["si_pct_ff"] = si.get("si_pct_ff")
            r["si_pct_chg"] = si.get("si_pct_chg")
            r["ctb"] = ortex_ctb_latest(t, log_fn=log_fn)
            r["avail"] = ortex_availability_latest(t, log_fn=log_fn)

            feat = dict(r)
            feat["window"] = window
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

    squeezes_true = [r for r in analyzed if r.get("bucket") == "SQUEEZE"]

    if squeezes_true:
        squeezes = sorted(
            squeezes_true,
            key=lambda r: (r.get("confidence", 0), r.get("base_score", 0)),
            reverse=True
        )[:TOP_N_PER_BUCKET]
    else:
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
