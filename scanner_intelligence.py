from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def _score_label(score: float) -> str:
    if score >= 85:
        return "Strong"
    if score >= 70:
        return "Good"
    if score >= 55:
        return "Mixed"
    return "Weak"


def _grade(score: float) -> str:
    if score >= 95:
        return "A+"
    if score >= 88:
        return "A"
    if score >= 75:
        return "B"
    if score >= 60:
        return "C"
    return "D"


def _risk_flag(risk_quality: float) -> str:
    if risk_quality >= 82:
        return "Low Risk"
    if risk_quality >= 64:
        return "Medium Risk"
    if risk_quality >= 45:
        return "High Risk"
    return "Extreme Risk"


@dataclass(frozen=True)
class IntelligenceWeights:
    momentum: float = 0.27
    liquidity: float = 0.20
    structure: float = 0.22
    squeeze: float = 0.13
    risk: float = 0.18


DEFAULT_WEIGHTS = IntelligenceWeights()


def classify_setup(row: dict[str, Any]) -> str:
    subtype = (row.get("subtype") or "").strip()
    bucket = (row.get("bucket") or "").upper()
    move = _num(row.get("move_pct"))
    relv = _num(row.get("rel_vol"))
    hold = _num(row.get("hold_pct"))
    close = _num(row.get("close"))
    vwap = _num(row.get("vwap"), default=0.0)
    range_pct = _num(row.get("range_pct"))

    if bucket == "SQUEEZE" and "watch" not in subtype.lower():
        return "Squeeze Expansion"
    if bucket == "SQUEEZE":
        return "Squeeze Watch"
    if vwap > 0 and close > 0 and abs(close - vwap) / close <= 0.02:
        return "VWAP Reclaim"
    if move >= 0.12 and relv >= 2.0 and hold >= 0.55:
        return "Momentum Breakout"
    if move >= 0.06 and range_pct >= 0.04 and hold >= 0.40:
        return "Continuation"
    if hold < 0.25 and move > 0:
        return "Reversal Watch"
    return subtype or "Momentum Setup"


def trend_alignment(row: dict[str, Any]) -> str:
    hold = _num(row.get("hold_pct"))
    move = _num(row.get("move_pct"))
    close = _num(row.get("close"))
    vwap = _num(row.get("vwap"), default=0.0)

    if move > 0 and hold >= 0.55 and (not vwap or close >= vwap):
        return "Intraday Trend Aligned"
    if move > 0 and hold >= 0.35:
        return "Constructive"
    if move > 0 and hold < 0.25:
        return "Countertrend / Fading"
    if move < 0:
        return "Bearish / Weak"
    return "Choppy"


def session_status(window: str | None) -> str:
    w = (window or "").upper()
    if "PREMARKET" in w:
        return "Premarket"
    if "AFTERHOURS" in w:
        return "After Hours"
    if "REGULAR" in w:
        return "Regular Session"
    return w.title() if w else "Session Unknown"


def _momentum_score(row: dict[str, Any]) -> tuple[float, str]:
    relv = _num(row.get("rel_vol"))
    move = abs(_num(row.get("move_pct")))
    hold = _num(row.get("hold_pct"))
    range_pct = _num(row.get("range_pct"))
    score = 20
    score += min(relv, 6.0) * 9
    score += min(move, 0.25) * 120
    score += hold * 24
    score += min(range_pct, 0.15) * 80
    score = _clamp(score)
    explanation = "Relative volume, price movement, range expansion, and candle hold quality."
    return score, explanation


def _liquidity_score(row: dict[str, Any]) -> tuple[float, str]:
    dollar_vol = _num(row.get("dollar_vol"))
    vol = _num(row.get("vol"))
    close = _num(row.get("close"))
    score = 20
    if dollar_vol >= 10_000_000:
        score += 55
    elif dollar_vol >= 3_000_000:
        score += 45
    elif dollar_vol >= 1_000_000:
        score += 32
    elif dollar_vol >= 300_000:
        score += 18
    else:
        score += 5
    if vol >= 1_000_000:
        score += 16
    elif vol >= 300_000:
        score += 10
    if close >= 1:
        score += 6
    score = _clamp(score)
    explanation = "Dollar volume, share volume, and basic tradability proxy."
    return score, explanation


def _structure_score(row: dict[str, Any]) -> tuple[float, str]:
    hold = _num(row.get("hold_pct"))
    range_pct = _num(row.get("range_pct"))
    close = _num(row.get("close"))
    vwap = _num(row.get("vwap"), default=0.0)
    score = 25 + hold * 42
    if vwap > 0 and close >= vwap:
        score += 16
    if 0.02 <= range_pct <= 0.12:
        score += 12
    elif range_pct > 0.18:
        score -= 10
    score = _clamp(score)
    explanation = "Trend hold, VWAP relationship, and clean range structure."
    return score, explanation


def _squeeze_score(row: dict[str, Any]) -> tuple[float, str]:
    si = row.get("si_pct_ff")
    ctb = row.get("ctb")
    avail = row.get("avail")
    float_shares = _num(row.get("float_shares"))
    status = (row.get("ortex_status") or "").lower()
    score = 25
    if si is not None:
        score += min(_num(si), 30) * 1.4
    if ctb is not None:
        score += min(_num(ctb), 200) * 0.12
    if avail is not None:
        av = _num(avail)
        if av <= 50_000:
            score += 18
        elif av <= 250_000:
            score += 12
        elif av <= 1_000_000:
            score += 5
    if 0 < float_shares <= 20_000_000:
        score += 12
    elif 0 < float_shares <= 100_000_000:
        score += 6
    if "confirmed" in status:
        score += 8
    if "missing" in status or "not used" in status:
        score -= 8
    score = _clamp(score)
    explanation = "Short interest, borrow cost, availability, float, and ORTEX confirmation."
    return score, explanation


def _risk_quality_score(row: dict[str, Any]) -> tuple[float, str]:
    dollar_vol = _num(row.get("dollar_vol"))
    range_pct = _num(row.get("range_pct"))
    move = abs(_num(row.get("move_pct")))
    close = _num(row.get("close"))
    vwap = _num(row.get("vwap"), default=0.0)
    float_shares = _num(row.get("float_shares"))
    score = 78
    if dollar_vol < 300_000:
        score -= 28
    elif dollar_vol < 1_000_000:
        score -= 12
    if range_pct > 0.18:
        score -= 22
    elif range_pct > 0.10:
        score -= 10
    if move > 0.35:
        score -= 20
    elif move > 0.20:
        score -= 10
    if vwap > 0 and close > 0 and close > vwap * 1.12:
        score -= 12
    if 0 < float_shares <= 5_000_000:
        score -= 8
    score = _clamp(score)
    explanation = "Execution cleanliness, liquidity, extension, volatility, and low-float risk."
    return score, explanation


def build_strengths(row: dict[str, Any], scores: dict[str, dict[str, Any]]) -> list[str]:
    strengths: list[str] = []
    if scores["momentum"]["score"] >= 75:
        strengths.append("Strong momentum and relative volume confirmation")
    if scores["liquidity"]["score"] >= 75:
        strengths.append("Liquidity appears favorable for cleaner execution")
    if scores["structure"]["score"] >= 75:
        strengths.append("Market structure is constructive and holding well")
    if scores["squeeze"]["score"] >= 70:
        strengths.append("Short/float context supports squeeze potential")
    if scores["risk"]["score"] >= 75:
        strengths.append("Risk profile is relatively clean for an active setup")
    catalyst = row.get("catalyst_label")
    if catalyst and catalyst not in ("Catalyst Not Verified", "No Clear Catalyst"):
        strengths.append(f"Catalyst context present: {catalyst}")
    return strengths or ["Some momentum characteristics are present, but confluence is still developing"]


def build_weaknesses(row: dict[str, Any], scores: dict[str, dict[str, Any]]) -> list[str]:
    weaknesses: list[str] = []
    if scores["risk"]["score"] < 55:
        weaknesses.append("Execution risk is elevated; avoid chasing without confirmation")
    if scores["liquidity"]["score"] < 60:
        weaknesses.append("Liquidity may be thin, increasing spread and slippage risk")
    if scores["structure"]["score"] < 60:
        weaknesses.append("Structure is choppy or not clearly confirmed")
    if scores["squeeze"]["score"] < 55:
        weaknesses.append("Squeeze confirmation is weak or incomplete")
    if abs(_num(row.get("move_pct"))) > 0.25:
        weaknesses.append("Move is extended; wait for a cleaner entry or pullback confirmation")
    return weaknesses or ["No major rule-based warning, but confirm price action before execution"]


def build_labels(row: dict[str, Any], scores: dict[str, dict[str, Any]]) -> list[str]:
    labels: list[str] = []
    setup = classify_setup(row)
    labels.append(setup)
    if scores["momentum"]["score"] >= 80:
        labels.append("Momentum Leader")
    if scores["squeeze"]["score"] >= 70:
        labels.append("Squeeze Candidate")
    if scores["structure"]["score"] >= 75:
        labels.append("Trend Aligned")
    if scores["risk"]["score"] < 55:
        labels.append("High Volatility")
    if scores["liquidity"]["score"] < 55:
        labels.append("Low Liquidity Risk")
    catalyst = row.get("catalyst_label")
    if catalyst and catalyst not in ("Catalyst Not Verified", "No Clear Catalyst"):
        labels.append(catalyst)
    return list(dict.fromkeys(labels))


def build_execution_notes(row: dict[str, Any], risk_flag: str) -> list[str]:
    trigger = _num(row.get("trigger"))
    stop = _num(row.get("stop"))
    close = _num(row.get("close"))
    vwap = _num(row.get("vwap"), default=0.0)
    notes = []
    if trigger > 0:
        notes.append(f"Entry logic: watch for confirmation around breakout/trigger near {trigger:.4f}, not blind chasing.")
    if stop > 0:
        notes.append(f"Invalidation logic: structure weakens if price loses the planned stop/support near {stop:.4f}.")
    if vwap > 0 and close > vwap * 1.10:
        notes.append("Extension warning: price is materially above VWAP; pullback/retest confirmation is cleaner than chasing.")
    notes.append(f"Risk context: {risk_flag}; size should reflect volatility, liquidity, and clarity of invalidation.")
    notes.append("Journal note: tag this as watched, traded, or skipped so outcome quality can be reviewed later.")
    return notes


def enrich_row(row: dict[str, Any], weights: IntelligenceWeights = DEFAULT_WEIGHTS) -> dict[str, Any]:
    momentum, momentum_ex = _momentum_score(row)
    liquidity, liquidity_ex = _liquidity_score(row)
    structure, structure_ex = _structure_score(row)
    squeeze, squeeze_ex = _squeeze_score(row)
    risk, risk_ex = _risk_quality_score(row)

    subscores = {
        "momentum": {"score": round(momentum), "label": _score_label(momentum), "explanation": momentum_ex},
        "liquidity": {"score": round(liquidity), "label": _score_label(liquidity), "explanation": liquidity_ex},
        "structure": {"score": round(structure), "label": _score_label(structure), "explanation": structure_ex},
        "squeeze": {"score": round(squeeze), "label": _score_label(squeeze), "explanation": squeeze_ex},
        "risk": {"score": round(risk), "label": _score_label(risk), "explanation": risk_ex},
    }

    confluence = (
        momentum * weights.momentum
        + liquidity * weights.liquidity
        + structure * weights.structure
        + squeeze * weights.squeeze
        + risk * weights.risk
    )
    confluence = _clamp(confluence)
    risk_flag = _risk_flag(risk)
    setup = classify_setup(row)
    labels = build_labels(row, subscores)
    strengths = build_strengths(row, subscores)
    weaknesses = build_weaknesses(row, subscores)
    trend = trend_alignment(row)

    brief = (
        f"{setup} profile with {_score_label(momentum).lower()} momentum quality and "
        f"{_score_label(liquidity).lower()} liquidity. "
        f"Structure is {_score_label(structure).lower()} and squeeze context is {_score_label(squeeze).lower()}. "
        f"Primary risk read: {risk_flag}."
    )

    raw = {
        "weights": weights.__dict__,
        "inputs": {
            "rel_vol": row.get("rel_vol"),
            "dollar_vol": row.get("dollar_vol"),
            "range_pct": row.get("range_pct"),
            "hold_pct": row.get("hold_pct"),
            "move_pct": row.get("move_pct"),
            "float_shares": row.get("float_shares"),
            "si_pct_ff": row.get("si_pct_ff"),
            "ctb": row.get("ctb"),
            "avail": row.get("avail"),
        },
    }

    return {
        "setup_grade": _grade(confluence),
        "setup_type": setup,
        "confluence_score": round(confluence),
        "risk_flag": risk_flag,
        "trend_alignment": trend,
        "session_status": session_status(row.get("window")),
        "subscores": subscores,
        "context_labels": labels,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "intelligence_brief": brief,
        "execution_notes": build_execution_notes(row, risk_flag),
        "raw_scoring_factors": raw,
    }


def enrich_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        enriched = dict(row)
        enriched["intelligence"] = enrich_row(enriched)
        out.append(enriched)
    return out
