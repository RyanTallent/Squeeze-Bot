import os
import csv
import json
import math
import time
import argparse
import requests
from pathlib import Path
from datetime import datetime, timedelta, timezone

# ============================================================
# ENGINE V1 — LOCKED SETTINGS (Ryan's decisions)
# ============================================================
SCAN_START_CT = (3, 0)     # 3:00 CT
SCAN_END_CT   = (8, 29)    # 8:29 CT
SCAN_EVERY_SECONDS = 5 * 60

TOP_N_PER_BUCKET = 3

# Base structure filters
MIN_PM_DOLLAR_VOL = 150_000
MIN_RANGE_PCT = 0.02
MIN_HOLD_PCT = 0.25

# Liquidity grades (pm $ volume)
LIQ_A = 1_500_000
LIQ_B = 500_000
LIQ_C = 150_000

# DNC (conservative)
CHASE_ABOVE_TRIGGER_PCT = 0.02
DNC_GAP_PCT = 0.20
DNC_RANGE_PCT = 0.10
DNC_THIN_LIQ = 250_000
DNC_SPIKE_RANGE = 0.07
DNC_SPIKE_HOLD = 0.22

# Halt
HALT_HIGH_RISK = 0.55
HALT_WATCH = 0.40

# TRUE SQUEEZE (strict) requires borrow pressure confirmation
REQUIRE_BORROW_DATA_FOR_TRUE_SQUEEZE = True

# Candidate selection
PRICE_TIERS = [(0.01, 2.50), (2.50, 5.00), (5.00, 10.00)]
MAX_CANDIDATES_PER_TIER = 300  # keep manageable

# ============================================================
# KEYS + SESSION
# ============================================================
POLYGON_KEY = os.getenv("POLYGON_API_KEY")
ORTEX_KEY = os.getenv("ORTEX_API_KEY")

if not POLYGON_KEY:
    raise RuntimeError("POLYGON_API_KEY environment variable is not set")
if not ORTEX_KEY:
    raise RuntimeError("ORTEX_API_KEY environment variable is not set")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "squeeze-bot/engine-v1"})

OUT_DIR = Path("outputs")
OUT_DIR.mkdir(exist_ok=True)

# ============================================================
# TIMEZONE (Central)
# ============================================================
try:
    from zoneinfo import ZoneInfo  # py 3.9+
    CT_TZ = ZoneInfo("America/Chicago")
except Exception:
    CT_TZ = timezone(timedelta(hours=-6))

# ============================================================
# HELPERS
# ============================================================
def now_ct() -> datetime:
    return datetime.now(tz=CT_TZ)

def ct_date_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")

def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))

def safe_float(x):
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None

def safe_int(x):
    try:
        if x is None:
            return None
        return int(float(x))
    except Exception:
        return None

def fmt_millions(n):
    if n is None:
        return "NA"
    try:
        n = int(n)
    except Exception:
        return "NA"
    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.2f}K"
    return str(n)

def sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)

def score_to_prob(score: float) -> float:
    return sigmoid((score - 55.0) / 12.0)

def polygon_get(url: str, params: dict | None = None) -> dict:
    params = dict(params or {})
    params["apiKey"] = POLYGON_KEY
    r = SESSION.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def ortex_get(url: str) -> dict:
    r = SESSION.get(url, headers={"Ortex-Api-Key": ORTEX_KEY}, timeout=30)
    r.raise_for_status()
    return r.json()

def is_weekday_ct(dt: datetime) -> bool:
    return dt.weekday() < 5

def ms_to_utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)

def ct_dt(date_str: str, hm: tuple[int, int]) -> datetime:
    y, m, d = map(int, date_str.split("-"))
    return datetime(y, m, d, hm[0], hm[1], tzinfo=CT_TZ)

def scan_window_utc(date_str: str, end_local: datetime) -> tuple[datetime, datetime, int]:
    start_local = ct_dt(date_str, SCAN_START_CT)

    # clamp end_local so it can’t be before start_local
    if end_local < start_local:
        end_local = start_local + timedelta(minutes=1)

    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)
    minutes = int(max(1, (end_local - start_local).total_seconds() // 60))
    return start_utc, end_utc, minutes


def next_5min_boundary(dt: datetime) -> datetime:
    minute = dt.minute
    add = (5 - (minute % 5)) % 5
    if add == 0 and dt.second == 0:
        add = 5
    return (dt.replace(second=0, microsecond=0) + timedelta(minutes=add))

# ============================================================
# POLYGON DATA
# ============================================================
def get_snapshot_all_tickers() -> list[dict]:
    data = polygon_get("https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers", {})
    return data.get("tickers", []) or []

def is_common_stock_polygon(ticker: str) -> bool:
    try:
        data = polygon_get(f"https://api.polygon.io/v3/reference/tickers/{ticker}", {})
        res = data.get("results") or {}
        return res.get("type") == "CS" and res.get("active") is True
    except Exception:
        return False

def polygon_shares_outstanding_best_effort(ticker: str) -> int | None:
    try:
        data = polygon_get(f"https://api.polygon.io/v3/reference/tickers/{ticker}", {})
        res = data.get("results") or {}
        for k in ("share_class_shares_outstanding", "weighted_shares_outstanding", "shares_outstanding"):
            v = safe_int(res.get(k))
            if v and v > 0:
                return v
    except Exception:
        return None
    return None

def pick_candidates_from_snapshot(snap: list[dict], max_candidates: int, price_min: float, price_max: float) -> list[str]:
    scored = []
    for item in snap:
        t = item.get("ticker")
        if not t:
            continue
        last_trade = item.get("lastTrade") or {}
        last_price = safe_float(last_trade.get("p"))
        if last_price is None or not (price_min <= last_price <= price_max):
            continue

        day = item.get("day") or {}
        prev = item.get("prevDay") or {}

        day_vol = safe_float(day.get("v")) or 0.0
        prev_close = safe_float(prev.get("c"))
        gap = 0.0
        if prev_close and prev_close > 0:
            gap = (last_price - prev_close) / prev_close

        dollar_vol_proxy = day_vol * last_price
        proxy = (dollar_vol_proxy / 1_000_000.0) + (abs(gap) * 4.0)
        scored.append((proxy, t))

    scored.sort(reverse=True, key=lambda x: x[0])
    return [t for _, t in scored[:max_candidates]]

def get_minute_aggs(ticker: str, date_str: str) -> list[dict]:
    url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/minute/{date_str}/{date_str}"
    data = polygon_get(url, {"adjusted": "true", "sort": "asc", "limit": 50000})
    return data.get("results", []) or []

def get_prev_close(ticker: str) -> float | None:
    try:
        one = polygon_get(f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}", {})
        prev = (one.get("ticker") or {}).get("prevDay") or {}
        return safe_float(prev.get("c"))
    except Exception:
        return None

def get_avg_daily_volume_10d(ticker: str, end_date_str: str) -> float | None:
    end_dt = datetime.fromisoformat(end_date_str)
    start_dt = end_dt - timedelta(days=20)
    start_str = start_dt.strftime("%Y-%m-%d")
    url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{start_str}/{end_date_str}"
    try:
        data = polygon_get(url, {"adjusted": "true", "sort": "asc", "limit": 50000})
        bars = data.get("results", []) or []
    except Exception:
        return None

    vols = []
    for b in bars[-10:]:
        v = safe_float(b.get("v"))
        if v is not None:
            vols.append(v)
    if not vols:
        return None
    return sum(vols) / len(vols)

def get_premarket_stats_dynamic(ticker: str, date_str: str, end_local: datetime) -> dict | None:
    results = get_minute_aggs(ticker, date_str)
    if not results:
        return None

    start_utc, end_utc, minutes = scan_window_utc(date_str, end_local)
    pm = [r for r in results if start_utc <= ms_to_utc(r["t"]) < end_utc]
    if not pm:
        return None

    pm_open = float(pm[0]["o"])
    pm_high = float(max(r["h"] for r in pm))
    pm_low = float(min(r["l"] for r in pm))
    pm_close = float(pm[-1]["c"])
    pm_vol = float(sum(r["v"] for r in pm))
    pm_dollar_vol = pm_vol * pm_close

    pm_range = pm_high - pm_low
    pm_range_pct = pm_range / max(pm_low, 1e-9)
    pm_hold_pct = (pm_close - pm_low) / max(pm_range, 1e-9)

    return {
        "pm_open": pm_open,
        "pm_high": pm_high,
        "pm_low": pm_low,
        "pm_close": pm_close,
        "pm_vol": pm_vol,
        "pm_dollar_vol": pm_dollar_vol,
        "pm_range_pct": pm_range_pct,
        "pm_hold_pct": pm_hold_pct,
        "pm_minutes": minutes,
    }

# ============================================================
# ORTEX DATA
# ============================================================
def ortex_short_interest_features(ticker: str) -> dict | None:
    try:
        data = ortex_get(f"https://api.ortex.com/api/v1/stock/US/{ticker}/short_interest")
    except Exception:
        return None

    rows = data.get("rows", []) or []
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

    float_est = None
    if si_shares is not None and si_pct is not None and si_pct > 0:
        est = si_shares / (si_pct / 100.0)
        if est > 0:
            float_est = int(est)

    return {"si_pct_ff": si_pct, "si_pct_chg": si_pct_chg, "si_shares": si_shares, "float_est": float_est}

def ortex_ctb_latest(ticker: str) -> float | None:
    endpoints = [
        f"https://api.ortex.com/api/v1/stock/US/{ticker}/ctb/all",
        f"https://api.ortex.com/api/v1/stock/US/{ticker}/ctb",
        f"https://api.ortex.com/api/v1/stock/US/{ticker}/borrow_cost/all",
    ]
    for url in endpoints:
        try:
            data = ortex_get(url)
        except Exception:
            continue
        rows = data.get("rows")
        if isinstance(rows, list) and rows:
            latest = rows[-1]
            for k in ("ctbAvg", "ctbAverage", "average", "borrowCostAvg", "borrowCostAverage"):
                v = safe_float(latest.get(k))
                if v is not None:
                    return v
    return None

def ortex_utilization_latest(ticker: str) -> float | None:
    endpoints = [
        f"https://api.ortex.com/api/v1/stock/US/{ticker}/utilization/all",
        f"https://api.ortex.com/api/v1/stock/US/{ticker}/utilization",
    ]
    for url in endpoints:
        try:
            data = ortex_get(url)
        except Exception:
            continue
        rows = data.get("rows")
        if isinstance(rows, list) and rows:
            latest = rows[-1]
            for k in ("utilization", "utilisation", "utilizationPct", "utilisationPct"):
                v = safe_float(latest.get(k))
                if v is not None:
                    return v
    return None

def ortex_availability_latest(ticker: str) -> float | None:
    endpoints = [
        f"https://api.ortex.com/api/v1/stock/US/{ticker}/availability/all",
        f"https://api.ortex.com/api/v1/stock/US/{ticker}/availability",
        f"https://api.ortex.com/api/v1/stock/US/{ticker}/borrow_availability/all",
    ]
    for url in endpoints:
        try:
            data = ortex_get(url)
        except Exception:
            continue
        rows = data.get("rows")
        if isinstance(rows, list) and rows:
            latest = rows[-1]
            for k in ("shares", "availableShares", "availabilityShares", "available", "avail"):
                v = safe_float(latest.get(k))
                if v is not None:
                    return v
    return None

# ============================================================
# ENGINE FEATURES + CLASSIFICATION
# ============================================================
def liquidity_grade(pm_dollar_vol: float) -> str:
    if pm_dollar_vol >= LIQ_A: return "A"
    if pm_dollar_vol >= LIQ_B: return "B"
    if pm_dollar_vol >= LIQ_C: return "C"
    return "D"

def compute_rel_vol(pm_vol: float, avg_daily_vol: float | None, pm_minutes: int) -> float | None:
    if avg_daily_vol is None or avg_daily_vol <= 0:
        return None
    expected = avg_daily_vol * (pm_minutes / 390.0)
    if expected <= 0:
        return None
    return pm_vol / expected

def compute_pressure_score(feat: dict) -> float:
    si = feat.get("si_pct_ff") or 0.0
    si_chg = feat.get("si_pct_chg") or 0.0
    ctb = feat.get("ctb") or 0.0
    util = feat.get("util") or 0.0
    avail = feat.get("avail")

    avail_term = 0.0
    if avail is not None:
        avail_term = max(0.0, 6.0 - math.log10(max(avail, 1.0)))

    score = 0.0
    score += si * 2.0
    score += si_chg * 8.0
    score += min(ctb, 200.0) * 0.10
    score += min(util, 100.0) * 0.08
    score += avail_term * 2.0
    return score

def compute_opportunity_score(feat: dict) -> float:
    rng = feat.get("pm_range_pct") or 0.0
    gap = feat.get("gap_pct") or 0.0
    relv = feat.get("rel_vol") or 0.0
    dv = feat.get("pm_dollar_vol") or 0.0
    dv_m = dv / 1_000_000.0

    score = 0.0
    score += rng * 120.0
    score += max(gap, 0.0) * 40.0
    score += min(relv, 6.0) * 10.0
    score += min(dv_m, 15.0) * 4.0
    return score

def compute_structure_score(feat: dict) -> float:
    hold = feat.get("pm_hold_pct") or 0.0
    rng = feat.get("pm_range_pct") or 0.0
    trigger = feat.get("trigger") or 0.0
    stop = feat.get("stop") or 0.0
    pm_close = feat.get("pm_close") or 0.0

    risk = max(trigger - stop, 1e-9)
    reward = max((rng * max(pm_close, 1e-9)), 1e-9)
    rr = reward / (risk / max(pm_close, 1e-9))

    score = 0.0
    score += hold * 60.0
    score += clamp(rr / 5.0) * 30.0
    score += clamp((0.12 - abs(rng - 0.06)) / 0.12) * 10.0
    return score

def halt_probability(feat: dict) -> float:
    price = safe_float(feat.get("pm_close")) or safe_float(feat.get("pm_open")) or 0.0
    gap = safe_float(feat.get("gap_pct")) or 0.0
    rng = safe_float(feat.get("pm_range_pct")) or 0.0
    relv = safe_float(feat.get("rel_vol")) or 0.0
    dv = safe_float(feat.get("pm_dollar_vol")) or 0.0
    flt = feat.get("float_shares")

    if price <= 1: pf = 0.35
    elif price <= 5: pf = 0.22
    elif price <= 10: pf = 0.15
    else: pf = 0.08

    move = (gap * 1.2) + (rng * 1.0) + clamp((relv - 1.0) / 8.0)
    move = clamp(move, 0.0, 1.0)

    if dv < 150_000: lf = 0.18
    elif dv < 500_000: lf = 0.10
    else: lf = 0.05

    if flt is not None and flt > 0:
        if flt < 10_000_000: ff = 0.18
        elif flt < 30_000_000: ff = 0.12
        elif flt < 80_000_000: ff = 0.07
        else: ff = 0.03
    else:
        ff = 0.06

    return clamp(pf + (0.55 * move) + lf + ff)

def do_not_chase_warning(feat: dict) -> tuple[bool, str]:
    gap = safe_float(feat.get("gap_pct")) or 0.0
    rng = safe_float(feat.get("pm_range_pct")) or 0.0
    hold = safe_float(feat.get("pm_hold_pct")) or 0.0
    dv = safe_float(feat.get("pm_dollar_vol")) or 0.0

    pm_close = safe_float(feat.get("pm_close")) or 0.0
    trigger = safe_float(feat.get("trigger")) or 0.0

    reasons = []
    if gap >= DNC_GAP_PCT: reasons.append("gap>=20%")
    if rng >= DNC_RANGE_PCT: reasons.append("range>=10%")
    if dv < DNC_THIN_LIQ: reasons.append("thin_liquidity")
    if rng >= DNC_SPIKE_RANGE and hold < DNC_SPIKE_HOLD: reasons.append("spike_risk")
    if trigger > 0 and pm_close > trigger * (1.0 + CHASE_ABOVE_TRIGGER_PCT): reasons.append(">2%_above_trigger")

    return (len(reasons) > 0), ",".join(reasons)

def data_quality_penalty(feat: dict) -> tuple[bool, str]:
    reasons = []
    if feat.get("rel_vol") is None: reasons.append("relVol_missing")
    if feat.get("float_shares") is None: reasons.append("float_missing")
    if feat.get("ctb") is None: reasons.append("ctb_missing")
    if feat.get("util") is None: reasons.append("util_missing")
    if feat.get("avail") is None: reasons.append("avail_missing")
    return (len(reasons) > 0), ",".join(reasons)

def is_true_squeeze_strict(feat: dict) -> bool:
    si = feat.get("si_pct_ff")
    if si is None or si < 8.0:
        return False
    if REQUIRE_BORROW_DATA_FOR_TRUE_SQUEEZE:
        if feat.get("ctb") is None or feat.get("util") is None or feat.get("avail") is None:
            return False
    util = feat.get("util")
    if util is not None and util < 80.0:
        return False
    avail = feat.get("avail")
    if avail is not None and avail > 250_000:
        return False
    if (feat.get("pm_dollar_vol") or 0.0) < 250_000:
        return False
    if (feat.get("pm_hold_pct") or 0.0) < 0.25:
        return False
    if (feat.get("pm_range_pct") or 0.0) < 0.02:
        return False
    return True

def setup_type_and_plan(feat: dict, squeeze: bool, dnc: bool, halt_p: float) -> tuple[str, str]:
    hold = feat.get("pm_hold_pct") or 0.0
    rng = feat.get("pm_range_pct") or 0.0
    dv = feat.get("pm_dollar_vol") or 0.0
    trigger = feat.get("trigger") or 0.0
    stop = feat.get("stop") or 0.0

    if halt_p >= HALT_HIGH_RISK:
        return ("halt-risk scalp",
                f"High halt risk. Size down. If trading: only quick scalps; avoid chasing. Trigger {trigger:.4f}, stop {stop:.4f}.")

    if squeeze:
        if dnc:
            return ("squeeze pullback",
                    f"DNC flagged—wait for pullback/flag then reclaim. Use trigger {trigger:.4f}. Stop {stop:.4f}.")
        return ("squeeze breakout",
                f"Clean squeeze setup. Entry near trigger {trigger:.4f}. Stop {stop:.4f}. Avoid >2% chase.")

    if dv < 300_000:
        return ("thin momentum",
                f"Thin liquidity—use limits only. Prefer pullback entry. Trigger {trigger:.4f}, stop {stop:.4f}.")

    if hold >= 0.35 and 0.02 <= rng <= 0.12:
        return ("momentum breakout",
                f"Entry near trigger {trigger:.4f}. Stop {stop:.4f}. Take profit into spikes; don’t chase >2%.")

    return ("momentum pullback",
            f"Wait for pullback/base then reclaim trigger {trigger:.4f}. Stop {stop:.4f}. Avoid FOMO entries.")

def confidence_grade(base_score: float, prob: float, liq_grade: str, halt_p: float, dnc: bool, low_quality: bool) -> str:
    pts = 0
    if base_score >= 120: pts += 3
    elif base_score >= 80: pts += 2
    elif base_score >= 50: pts += 1

    if prob >= 0.85: pts += 2
    elif prob >= 0.65: pts += 1

    if halt_p >= HALT_HIGH_RISK: pts -= 2
    elif halt_p >= HALT_WATCH: pts -= 1
    if dnc: pts -= 1
    if low_quality: pts -= 1

    if pts >= 4: conf = "A"
    elif pts >= 2: conf = "B"
    else: conf = "C"

    if conf == "A" and liq_grade in ("C", "D"):
        conf = "B"
    return conf

# ============================================================
# REPORTING (HTML + CSV + JSON)
# ============================================================
def write_reports(date_str: str, end_local: datetime, top_squeeze: list[dict], top_momentum: list[dict], meta: dict):
    ts = now_ct().strftime("%Y-%m-%d_%H-%M-%S")
    base = f"scan_{date_str}_{ts}"

    txt_path = OUT_DIR / f"{base}.txt"
    csv_path = OUT_DIR / f"{base}.csv"
    json_path = OUT_DIR / f"{base}.json"
    html_path = OUT_DIR / f"{base}.html"

    def fmt_num(x, nd=2):
        if x is None: return "NA"
        try: return f"{float(x):.{nd}f}"
        except Exception: return "NA"

    def fmt_int(x):
        if x is None: return "NA"
        try: return f"{int(float(x)):,}"
        except Exception: return "NA"

    def badge(text, kind):
        return f'<span class="badge {kind}">{text}</span>'

    def conf_kind(c):
        return "good" if c == "A" else ("warn" if c == "B" else "bad")

    def halt_kind(h):
        try: hv = float(h)
        except Exception: return "neutral"
        if hv >= HALT_HIGH_RISK: return "bad"
        if hv >= HALT_WATCH: return "warn"
        return "good"

    def dnc_kind(v):
        return "bad" if v == "YES" else "good"

    def row_view(r: dict):
        return {
            "Ticker": r.get("ticker"),
            "Bucket": r.get("bucket"),
            "Conf": r.get("confidence", "NA"),
            "Liq": r.get("liq_grade", "NA"),
            "DNC": "YES" if r.get("do_not_chase") else "NO",
            "Halt": fmt_num(r.get("halt_prob"), 3),
            "Float": fmt_millions(r.get("float_shares")),
            "pm$Vol": fmt_int(r.get("pm_dollar_vol")),
            "Gap%": fmt_num((r.get("gap_pct") or 0) * 100, 1),
            "Range%": fmt_num((r.get("pm_range_pct") or 0) * 100, 1),
            "Hold": fmt_num(r.get("pm_hold_pct"), 2),
            "SI%": fmt_num(r.get("si_pct_ff"), 1),
            "CTB": fmt_num(r.get("ctb"), 1),
            "Util": fmt_num(r.get("util"), 0),
            "Avail": fmt_int(r.get("avail")),
            "Trigger": fmt_num(r.get("trigger"), 4),
            "Stop": fmt_num(r.get("stop"), 4),
            "Setup": r.get("setup_type", ""),
            "Plan": (r.get("plan", "")[:120] + "…") if r.get("plan") and len(r.get("plan")) > 120 else (r.get("plan") or ""),
        }

    def table_html(rows):
        if not rows: return "<p class='muted'>None</p>"
        cols = list(rows[0].keys())
        head = "".join(f"<th>{c}</th>" for c in cols)
        body = ""
        for r in rows:
            cells = []
            for c in cols:
                v = r[c]
                if c == "Conf":
                    cells.append(f"<td>{badge(v, conf_kind(v))}</td>")
                elif c == "DNC":
                    cells.append(f"<td>{badge(v, dnc_kind(v))}</td>")
                elif c == "Halt":
                    cells.append(f"<td>{badge(v, halt_kind(v))}</td>")
                elif c == "Liq":
                    kind = "good" if v == "A" else ("warn" if v == "B" else "bad")
                    cells.append(f"<td>{badge(v, kind)}</td>")
                else:
                    cells.append(f"<td>{v}</td>")
            body += "<tr>" + "".join(cells) + "</tr>"
        return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"

    sq = [row_view(r) for r in top_squeeze]
    mo = [row_view(r) for r in top_momentum]

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"Date: {date_str}\n")
        f.write(f"Scan window: {ct_dt(date_str, SCAN_START_CT).strftime('%H:%M')}–{end_local.strftime('%H:%M')} CT\n")
        flt = meta["filters"]
        f.write(f"Filters: pm_$vol>={int(flt['min_pm_dollar_vol'])}, range_pct>={flt['min_range_pct']}, hold_pct>={flt['min_hold_pct']}\n\n")

        def write_bucket(title, rows):
            f.write(f"{title} (Top {len(rows)}):\n")
            if not rows:
                f.write("  none\n\n")
                return
            for i, r in enumerate(rows, 1):
                f.write(
                    f"  {i}) {r['ticker']} | CONF={r.get('confidence')} LIQ={r.get('liq_grade')} "
                    f"| DNC={'YES' if r.get('do_not_chase') else 'NO'} | HALT={r.get('halt_prob'):.3f} "
                    f"| TRG={r.get('trigger'):.4f} STOP={r.get('stop'):.4f} | {r.get('setup_type','')}\n"
                )
            f.write("\n")

        write_bucket("TRUE SQUEEZE", top_squeeze)
        write_bucket("MOMENTUM", top_momentum)
        f.write("Open the HTML report for the readable dashboard.\n")

    all_rows = top_squeeze + top_momentum
    fieldnames = sorted(set().union(*(r.keys() for r in all_rows)) or [])
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in all_rows:
            w.writerow({k: r.get(k) for k in fieldnames})

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "date": date_str, "top_squeeze": top_squeeze, "top_momentum": top_momentum}, f, indent=2)

    html = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>SqueezeBot Engine v1 — {date_str}</title>
<style>
body {{ font-family: Arial, sans-serif; margin: 24px; }}
h1 {{ margin-bottom: 4px; }}
.sub {{ color: #666; margin-bottom: 16px; }}
.card {{ border: 1px solid #e6e6e6; border-radius: 12px; padding: 14px; margin-bottom: 20px; }}
table {{ width: 100%; border-collapse: collapse; }}
th, td {{ padding: 8px 10px; border-bottom: 1px solid #eee; font-size: 14px; vertical-align: top; }}
th {{ background: #fafafa; position: sticky; top: 0; }}
.badge {{ padding: 3px 8px; border-radius: 999px; font-weight: 700; font-size: 12px; display: inline-block; }}
.good {{ background: #e8f7ec; color: #126b2c; }}
.warn {{ background: #fff5d6; color: #7a4b00; }}
.bad {{ background: #ffe6e6; color: #8a1f1f; }}
.neutral {{ background: #eee; color: #333; }}
.muted {{ color: #777; }}
small {{ color: #777; }}
</style>
</head>
<body>
<h1>SqueezeBot — Engine v1</h1>
<div class="sub">
  {date_str} • Window: {ct_dt(date_str, SCAN_START_CT).strftime('%H:%M')}–{end_local.strftime('%H:%M')} CT • Top {TOP_N_PER_BUCKET} per bucket
</div>

<div class="card">
  <h2>TRUE SQUEEZE</h2>
  <small>Strict squeeze: requires CTB + Util + Availability present.</small>
  {table_html(sq)}
</div>

<div class="card">
  <h2>MOMENTUM</h2>
  <small>DNC is conservative. “Chasing” = &gt;2% above trigger.</small>
  {table_html(mo)}
</div>

</body>
</html>
"""
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    return txt_path, csv_path, json_path, html_path

# ============================================================
# SCAN ENGINE (one scan pass)
# ============================================================
def run_single_scan(date_str: str, end_local: datetime):
    snap_all = get_snapshot_all_tickers()

    meta = {
        "date": date_str,
        "scan_end_ct": end_local.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "filters": {
            "min_pm_dollar_vol": MIN_PM_DOLLAR_VOL,
            "min_range_pct": MIN_RANGE_PCT,
            "min_hold_pct": MIN_HOLD_PCT,
        },
        "tiers": PRICE_TIERS,
        "top_n_per_bucket": TOP_N_PER_BUCKET,
    }

    rows = []

    for pmin, pmax in PRICE_TIERS:
        candidates = pick_candidates_from_snapshot(
            snap_all,
            max_candidates=MAX_CANDIDATES_PER_TIER,
            price_min=pmin,
            price_max=pmax,
        )

        for t in candidates:
            try:
                if not is_common_stock_polygon(t):
                    continue

                pm = get_premarket_stats_dynamic(t, date_str, end_local)
                if not pm:
                    continue

                if pm["pm_dollar_vol"] < MIN_PM_DOLLAR_VOL:
                    continue
                if pm["pm_range_pct"] < MIN_RANGE_PCT:
                    continue
                if pm["pm_hold_pct"] < MIN_HOLD_PCT:
                    continue

                prev_close = get_prev_close(t)
                gap_pct = 0.0
                if prev_close is not None and prev_close > 0:
                    gap_pct = (pm["pm_close"] - prev_close) / prev_close

                avg_vol_10d = get_avg_daily_volume_10d(t, date_str)
                relv = compute_rel_vol(pm["pm_vol"], avg_vol_10d, pm["pm_minutes"])

                si_feat = ortex_short_interest_features(t)
                if not si_feat or si_feat.get("si_pct_ff") is None:
                    continue

                ctb = ortex_ctb_latest(t)
                util = ortex_utilization_latest(t)
                avail = ortex_availability_latest(t)

                float_poly = polygon_shares_outstanding_best_effort(t)
                float_est = si_feat.get("float_est")
                float_shares = float_poly if float_poly is not None else float_est

                trigger = pm["pm_high"]
                stop = pm["pm_low"]

                feat = {
                    "ticker": t,
                    **pm,
                    **si_feat,
                    "ctb": ctb,
                    "util": util,
                    "avail": avail,
                    "float_shares": float_shares,
                    "gap_pct": gap_pct,
                    "rel_vol": relv,
                    "trigger": trigger,
                    "stop": stop,
                    "price_tier": f"{pmin:.2f}-{pmax:.2f}",
                }

                squeeze = is_true_squeeze_strict(feat)

                pressure = compute_pressure_score(feat)
                opportunity = compute_opportunity_score(feat)
                structure = compute_structure_score(feat)

                base_score = 0.34 * pressure + 0.33 * opportunity + 0.33 * structure
                prob = score_to_prob(base_score)

                hp = halt_probability(feat)
                dnc, dnc_reason = do_not_chase_warning(feat)
                low_q, low_q_reason = data_quality_penalty(feat)

                liq = liquidity_grade(pm["pm_dollar_vol"])
                setup, plan = setup_type_and_plan(feat, squeeze=squeeze, dnc=dnc, halt_p=hp)

                conf = confidence_grade(
                    base_score=base_score,
                    prob=prob,
                    liq_grade=liq,
                    halt_p=hp,
                    dnc=dnc,
                    low_quality=low_q,
                )

                bucket = "TRUE_SQUEEZE" if squeeze else "MOMENTUM"

                rows.append({
                    "ticker": t,
                    "bucket": bucket,
                    "base_score": base_score,
                    "prob": prob,
                    "pressure_score": pressure,
                    "opportunity_score": opportunity,
                    "structure_score": structure,
                    "si_pct_ff": feat.get("si_pct_ff"),
                    "si_pct_chg": feat.get("si_pct_chg"),
                    "si_shares": feat.get("si_shares"),
                    "ctb": ctb,
                    "util": util,
                    "avail": avail,
                    "float_shares": float_shares,
                    "gap_pct": gap_pct,
                    "rel_vol": relv,
                    "pm_dollar_vol": pm["pm_dollar_vol"],
                    "pm_vol": pm["pm_vol"],
                    "pm_range_pct": pm["pm_range_pct"],
                    "pm_hold_pct": pm["pm_hold_pct"],
                    "pm_open": pm["pm_open"],
                    "pm_high": pm["pm_high"],
                    "pm_low": pm["pm_low"],
                    "pm_close": pm["pm_close"],
                    "pm_minutes": pm["pm_minutes"],
                    "trigger": trigger,
                    "stop": stop,
                    "halt_prob": hp,
                    "do_not_chase": dnc,
                    "dnc_reason": dnc_reason,
                    "data_low_quality": low_q,
                    "data_quality_reason": low_q_reason,
                    "liq_grade": liq,
                    "confidence": conf,
                    "setup_type": setup,
                    "plan": plan,
                    "price_tier": feat.get("price_tier"),
                })

            except Exception:
                continue

    squeezes = [r for r in rows if r["bucket"] == "TRUE_SQUEEZE"]
    momentum = [r for r in rows if r["bucket"] == "MOMENTUM"]

    conf_rank = {"A": 3, "B": 2, "C": 1}
    liq_rank = {"A": 3, "B": 2, "C": 1, "D": 0}

    def sort_key(r):
        return (
            r["base_score"],
            conf_rank.get(r.get("confidence", "C"), 1),
            liq_rank.get(r.get("liq_grade", "D"), 0),
            -(r.get("halt_prob") or 0.0),
            r.get("pm_dollar_vol") or 0.0,
        )

    squeezes.sort(key=sort_key, reverse=True)
    momentum.sort(key=sort_key, reverse=True)

    return squeezes[:TOP_N_PER_BUCKET], momentum[:TOP_N_PER_BUCKET], meta

# ============================================================
# MODES
# ============================================================
def run_one_shot(force: bool = False):
    dt = now_ct()

    if not is_weekday_ct(dt):
        back = dt
        while back.weekday() >= 5:
            back = back - timedelta(days=1)
        dt = back

    date_str = ct_date_str(dt)
    start_local = ct_dt(date_str, SCAN_START_CT)
    end_local_limit = ct_dt(date_str, SCAN_END_CT)
    now_local = now_ct()

    if not force:
        if now_local < start_local:
            print(f"Outside scan window (too early). Window is {start_local.strftime('%H:%M')}–{end_local_limit.strftime('%H:%M')} CT.")
            return
        if now_local >= end_local_limit:
            print(f"Outside scan window (too late). Window is {start_local.strftime('%H:%M')}–{end_local_limit.strftime('%H:%M')} CT.")
            return

    end_local = min(now_local, end_local_limit)
    top_s, top_m, meta = run_single_scan(date_str, end_local)
    txt_path, csv_path, json_path, html_path = write_reports(date_str, end_local, top_s, top_m, meta)

    print(f"Saved TXT:  {txt_path}")
    print(f"Saved CSV:  {csv_path}")
    print(f"Saved JSON: {json_path}")
    print(f"Saved HTML: {html_path}")

def run_loop_forever():
    while True:
        try:
            dt = now_ct()
            if not is_weekday_ct(dt):
                time.sleep(60)
                continue

            date_str = ct_date_str(dt)
            start_local = ct_dt(date_str, SCAN_START_CT)
            end_local_limit = ct_dt(date_str, SCAN_END_CT)
            now_local = now_ct()

            if now_local < start_local:
                time.sleep(60)
                continue

            if now_local >= end_local_limit:
                time.sleep(300)
                continue

            end_local = min(now_local, end_local_limit)
            top_s, top_m, meta = run_single_scan(date_str, end_local)
            _, _, _, html_path = write_reports(date_str, end_local, top_s, top_m, meta)
            print(f"[{now_local.strftime('%H:%M:%S')}] Saved: {html_path}")

            nxt = next_5min_boundary(now_local)
            sleep_sec = max(1, (nxt - now_local).total_seconds())
            time.sleep(sleep_sec)

        except KeyboardInterrupt:
            print("Stopped.")
            break
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(10)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["oneshot", "loop"], default="loop")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args()

    if args.run_id:
        print(f"Run ID: {args.run_id}")

    if args.mode == "oneshot":
        run_one_shot(force=args.force)
    else:
        run_loop_forever()

if __name__ == "__main__":
    main()
