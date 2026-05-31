from __future__ import annotations

from datetime import datetime
from typing import Any


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        if value in (None, ""):
            return default
        val = float(value)
        return val if val == val else default
    except Exception:
        return default


def _clamp(score: float) -> float:
    return max(0.0, min(100.0, score))


def _rating(score: float) -> str:
    if score >= 82:
        return "Very High Conviction"
    if score >= 68:
        return "High Conviction"
    if score >= 52:
        return "Medium Conviction"
    if score >= 38:
        return "Low Conviction"
    return "Avoid / Watchlist Only"


def _framework_score(section: dict[str, Any], risk_oriented: bool = False) -> float:
    if section.get("score") is not None:
        score = _num(section.get("score"), 50)
        return _clamp(score or 50)
    rating = str(section.get("rating") or "").lower()
    if risk_oriented:
        if rating == "low":
            return 76
        if rating == "medium":
            return 55
        if rating == "high":
            return 25
    if rating in ("high", "undervalued"):
        return 76
    if rating in ("medium", "fairly valued"):
        return 56
    if rating in ("low", "expensive"):
        return 35
    if rating == "very expensive":
        return 18
    return 45


def _technical_score(profile: dict[str, Any]) -> tuple[float, list[str], list[str]]:
    metrics = (profile or {}).get("metrics") or {}
    latest = _num(metrics.get("latest_close"))
    sma50 = _num(metrics.get("sma50"))
    sma200 = _num(metrics.get("sma200"))
    period_return = _num(metrics.get("period_return"))
    rel_strength = _num(metrics.get("relative_strength_vs_spy"))
    volatility = _num(metrics.get("annualized_volatility"))
    drawdown = _num(metrics.get("max_drawdown"))
    score = 48.0
    reasons_for: list[str] = []
    reasons_against: list[str] = []

    if latest is not None and sma50 is not None:
        if latest >= sma50:
            score += 12
            reasons_for.append("Price is above the 50-day moving average.")
        else:
            score -= 10
            reasons_against.append("Price is below the 50-day moving average.")
    if latest is not None and sma200 is not None:
        if latest >= sma200:
            score += 14
            reasons_for.append("Price is above the 200-day moving average.")
        else:
            score -= 14
            reasons_against.append("Price is below the 200-day moving average.")
    if period_return is not None:
        if period_return > 0:
            score += min(12, period_return * 60)
            reasons_for.append(f"Selected-period return is positive ({period_return * 100:.1f}%).")
        else:
            score += max(-12, period_return * 60)
            reasons_against.append(f"Selected-period return is negative ({period_return * 100:.1f}%).")
    if rel_strength is not None:
        if rel_strength > 0:
            score += min(10, rel_strength * 80)
            reasons_for.append(f"Relative strength vs SPY is positive ({rel_strength * 100:.1f}%).")
        else:
            score += max(-10, rel_strength * 80)
            reasons_against.append(f"Relative strength vs SPY is negative ({rel_strength * 100:.1f}%).")
    if volatility is not None and volatility > 0.70:
        score -= 8
        reasons_against.append(f"Annualized volatility is elevated ({volatility * 100:.1f}%).")
    if drawdown is not None and drawdown < -0.30:
        score -= 8
        reasons_against.append(f"Range drawdown is material ({drawdown * 100:.1f}%).")
    return _clamp(score), reasons_for, reasons_against


def build_conviction(
    profile: dict[str, Any],
    fundamentals: dict[str, Any] | None,
    valuation: dict[str, Any] | None,
    peer_benchmarking: dict[str, Any] | None,
    research_scores: dict[str, Any] | None = None,
    data_coverage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sections = (fundamentals or {}).get("sections") or {}
    earnings = sections.get("earnings_quality") or {}
    analyst = sections.get("analyst_expectations") or {}
    balance = sections.get("balance_sheet") or {}
    margin = sections.get("margin_analysis") or {}

    technical_score, technical_for, technical_against = _technical_score(profile)
    fundamental_score = (_framework_score(earnings) * 0.55) + (_framework_score(margin) * 0.45)
    valuation_score = _framework_score(valuation or {})
    peer_score = _framework_score(peer_benchmarking or {})
    analyst_score = _framework_score(analyst)
    balance_score = _framework_score(balance, risk_oriented=True)

    weights = {
        "technicals": 0.22,
        "fundamentals": 0.22,
        "valuation": 0.18,
        "peer_benchmarking": 0.14,
        "analyst_expectations": 0.12,
        "balance_sheet": 0.12,
    }
    component_scores = {
        "technicals": technical_score,
        "fundamentals": fundamental_score,
        "valuation": valuation_score,
        "peer_benchmarking": peer_score,
        "analyst_expectations": analyst_score,
        "balance_sheet": balance_score,
    }
    score = sum(component_scores[k] * weights[k] for k in weights)

    reasons_for = list(technical_for)
    reasons_against = list(technical_against)
    for label, section, component in (
        ("Fundamentals", earnings, fundamental_score),
        ("Valuation", valuation or {}, valuation_score),
        ("Peer benchmarking", peer_benchmarking or {}, peer_score),
        ("Analyst expectations", analyst, analyst_score),
        ("Balance sheet", balance, balance_score),
    ):
        item = ((section or {}).get("items") or [None])[0]
        if component >= 62:
            reasons_for.append(f"{label}: {item or (section or {}).get('rating') or 'constructive evidence.'}")
        elif component <= 42:
            reasons_against.append(f"{label}: {item or (section or {}).get('rating') or 'weak evidence.'}")

    evidence_count = sum(1 for value in component_scores.values() if value is not None)
    coverage_score = _num((data_coverage or {}).get("score"), 0) or 0
    coverage_ceiling = _num((data_coverage or {}).get("confidence_ceiling"), coverage_score / 100) or 0.20
    base_confidence = min(0.90, 0.25 + evidence_count * 0.08)
    confidence = round(min(base_confidence, max(0.20, coverage_ceiling)), 2)
    return {
        "score": round(_clamp(score)),
        "rating": _rating(score),
        "reasons_for": reasons_for[:8] or ["No strong positive conviction driver detected yet."],
        "reasons_against": reasons_against[:8] or ["No major conviction objection detected from connected data."],
        "component_scores": {k: round(_clamp(v)) for k, v in component_scores.items()},
        "weights": weights,
        "confidence": confidence,
        "data_coverage_score": round(coverage_score),
        "data_coverage_rating": (data_coverage or {}).get("rating"),
        "source": "Deterministic Research Engine",
        "timestamp": datetime.utcnow().isoformat(),
        "calculation": {
            "formula": "Weighted score from technicals, fundamentals, valuation, peer benchmarking, analyst expectations, and balance sheet.",
            "inputs": {"component_scores": component_scores, "weights": weights},
            "rating_logic": "Higher scores indicate stronger evidence alignment; expensive valuation, weak peers, poor fundamentals, or balance-sheet risk reduce conviction.",
        },
    }
