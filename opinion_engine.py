from __future__ import annotations

from datetime import datetime
from typing import Any


STANCE_LABELS = ("Strong Buy Candidate", "Buy Candidate", "Hold / Watchlist", "Avoid for Now", "Reduce / Too Risky")


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        val = float(value)
        return val if val == val else default
    except Exception:
        return default


def _first(items: list[str] | None, fallback: str) -> str:
    return (items or [fallback])[0] or fallback


def _score_label(score: float) -> str:
    if score >= 80:
        return "Very High"
    if score >= 65:
        return "High"
    if score >= 50:
        return "Medium"
    if score >= 35:
        return "Low"
    return "Very Low"


def _stance_from_scores(conviction: float, valuation: float, coverage: float, peer: float, technical: float, balance: float) -> str:
    composite = conviction * 0.38 + valuation * 0.20 + coverage * 0.16 + peer * 0.12 + technical * 0.08 + balance * 0.06
    if coverage < 45:
        return "Hold / Watchlist" if composite >= 55 else "Avoid for Now"
    if conviction >= 78 and valuation >= 58 and coverage >= 70 and peer >= 55:
        return "Strong Buy Candidate"
    if composite >= 68 and conviction >= 62 and valuation >= 42:
        return "Buy Candidate"
    if valuation < 25 or conviction < 35:
        return "Avoid for Now"
    if composite < 42:
        return "Reduce / Too Risky"
    return "Hold / Watchlist"


def _trade_view(technical: float, liquidity: float, volatility: float, conviction: float, coverage: float) -> tuple[str, str]:
    trade_score = technical * 0.35 + liquidity * 0.25 + volatility * 0.15 + conviction * 0.15 + coverage * 0.10
    if coverage < 45:
        return "Watchlist only", "Trade judgment is limited because data coverage is incomplete."
    if trade_score >= 70:
        return "Good trade candidate", "Technicals, liquidity, and conviction are aligned enough to justify a trading watch."
    if trade_score >= 52:
        return "Conditional trade candidate", "Trade setup requires confirmation; evidence is not strong enough for aggressive action."
    return "Poor trade candidate", "Trade evidence is not strong enough; avoid forcing the setup."


def _long_term_view(conviction: float, fundamental: float, valuation: float, coverage: float, balance: float) -> tuple[str, str]:
    long_score = conviction * 0.32 + fundamental * 0.24 + valuation * 0.18 + balance * 0.14 + coverage * 0.12
    if coverage < 45:
        return "Unproven long-term candidate", "Long-term judgment should wait for better data coverage."
    if long_score >= 72:
        return "Good long-term investment candidate", "Business/fundamental quality and conviction support a constructive long-term view."
    if long_score >= 55:
        return "Selective long-term candidate", "Long-term case is plausible but not yet highest-conviction."
    return "Weak long-term candidate", "Long-term evidence is not strong enough relative to valuation, quality, or data gaps."


def build_opinion_intelligence(
    profile: dict[str, Any],
    scores: dict[str, Any],
    fundamentals: dict[str, Any] | None,
    valuation: dict[str, Any],
    peer_benchmarking: dict[str, Any],
    conviction: dict[str, Any],
    data_coverage: dict[str, Any],
    sector_framework: dict[str, Any],
    portfolio_context: dict[str, Any] | None = None,
    wealth_context: dict[str, Any] | None = None,
    committee_context: dict[str, Any] | None = None,
    risk_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ticker = profile.get("ticker") or (fundamentals or {}).get("ticker") or "Ticker"
    technical = _num((scores.get("trend_quality") or {}).get("score"), 50) * 0.55 + _num((scores.get("momentum_quality") or {}).get("score"), 50) * 0.45
    liquidity = _num((scores.get("liquidity_quality") or {}).get("score"), 50)
    volatility = _num((scores.get("volatility_risk") or {}).get("score"), 50)
    fundamental = _num((scores.get("fundamental_quality") or {}).get("score"), 50)
    balance = _num((conviction.get("component_scores") or {}).get("balance_sheet"), 50)
    conviction_score = _num(conviction.get("score"), 45)
    valuation_score = _num(valuation.get("score"), 45)
    peer_score = _num(peer_benchmarking.get("score"), 45)
    coverage_score = _num(data_coverage.get("score"), 35)
    missing = list(data_coverage.get("missing_data") or [])
    valuation_rating = valuation.get("rating") or "n/a"
    stance = _stance_from_scores(conviction_score, valuation_score, coverage_score, peer_score, technical, balance)
    long_label, long_reason = _long_term_view(conviction_score, fundamental, valuation_score, coverage_score, balance)
    trade_label, trade_reason = _trade_view(technical, liquidity, volatility, conviction_score, coverage_score)

    reasons_for = conviction.get("reasons_for") or []
    reasons_against = conviction.get("reasons_against") or []
    strongest_bull = _first(reasons_for, "No high-conviction bull argument is established yet.")
    strongest_bear = _first(reasons_against, "No high-conviction bear argument is established yet.")
    if valuation_score < 35 and "valuation" not in strongest_bear.lower():
        strongest_bear = f"Valuation is the main objection: {valuation_rating} ({valuation_score:.0f}/100)."

    key_risks = []
    if valuation_score < 45:
        key_risks.append(f"Valuation risk: {valuation_rating} ({valuation_score:.0f}/100).")
    if coverage_score < 60:
        key_risks.append(f"Data coverage is incomplete ({coverage_score:.0f}/100); missing: {', '.join(missing) or 'unspecified'}")
    if peer_score < 45:
        key_risks.append(f"Peer benchmarking is weak ({peer_score:.0f}/100).")
    key_risks.extend(reasons_against[:3])

    key_opportunities = []
    if conviction_score >= 65:
        key_opportunities.append(f"Conviction is constructive ({conviction_score:.0f}/100).")
    if peer_score >= 65:
        key_opportunities.append(f"Peer benchmarking is supportive ({peer_score:.0f}/100).")
    if valuation_score >= 60:
        key_opportunities.append(f"Valuation is not blocking the thesis ({valuation_rating}, {valuation_score:.0f}/100).")
    key_opportunities.extend(reasons_for[:3])

    what_matters = []
    if conviction_score >= 65:
        what_matters.append("Conviction is the primary positive signal.")
    if valuation_score < 45:
        what_matters.append("Valuation is the primary constraint on aggressiveness.")
    if coverage_score < 60:
        what_matters.append("Data coverage matters more than any single bullish metric.")
    if peer_score < 45:
        what_matters.append("Weak peer standing should limit confidence.")
    if not what_matters:
        what_matters.append("The balance between conviction, valuation, and data coverage matters most.")

    what_is_noise = [
        "Single-period price movement is noise unless it confirms trend, volume, or thesis change.",
        "A high conviction score is not enough by itself if valuation or data coverage is weak.",
    ]
    overlooked = []
    if valuation_score < 45 and conviction_score >= 70:
        overlooked.append("The market may be correctly pricing quality, but multiple compression risk is easy to overlook.")
    if coverage_score < 60:
        overlooked.append("The largest overlooked issue is evidence quality, not upside narrative.")
    if peer_score >= 65 and valuation_score >= 50:
        overlooked.append("Peer-relative strength may be more important than the headline multiple.")
    if not overlooked:
        overlooked.append("No clear market-overlooked factor is established from connected data yet.")

    change_mind = []
    if stance in ("Strong Buy Candidate", "Buy Candidate"):
        change_mind.extend([
            "Material deterioration in revenue, margins, cash flow, or balance-sheet evidence.",
            "Valuation moving to Very Expensive without offsetting growth or quality evidence.",
            "Peer rank weakening or analyst expectations deteriorating.",
        ])
    elif stance == "Hold / Watchlist":
        change_mind.extend([
            "Improved valuation or a pullback with fundamentals intact would raise conviction.",
            "Better data coverage and peer evidence would reduce uncertainty.",
            "Trend/relative strength breakdown would move the stance toward Avoid/Reduce.",
        ])
    else:
        change_mind.extend([
            "Valuation becoming reasonable relative to growth and peers.",
            "Data coverage improving enough to verify fundamentals and analyst expectations.",
            "Conviction and peer benchmarking improving materially.",
        ])

    confidence = min(_num(conviction.get("confidence"), 0.35), max(0.20, coverage_score / 100))
    if missing:
        confidence = min(confidence, 0.55)

    final_body = (
        f"Praetor's View: {ticker} is currently classified as {stance}. "
        f"Conviction is {_score_label(conviction_score).lower()} ({conviction_score:.0f}/100), valuation is {valuation_rating} "
        f"({valuation_score:.0f}/100), peer benchmarking is {_score_label(peer_score).lower()} ({peer_score:.0f}/100), "
        f"and data coverage is {data_coverage.get('rating', 'n/a')} ({coverage_score:.0f}/100). "
        f"The most important positive is: {strongest_bull} The most important objection is: {strongest_bear}"
    )

    return {
        "label": stance,
        "final_stance": stance,
        "buy_hold_avoid_view": stance,
        "long_term_investment_view": {"label": long_label, "body": long_reason},
        "trade_view": {"label": trade_label, "body": trade_reason},
        "conviction": {"score": round(conviction_score), "rating": conviction.get("rating"), "confidence": round(confidence, 2)},
        "what_would_change_my_mind": change_mind,
        "highest_conviction_bull_argument": strongest_bull,
        "highest_conviction_bear_argument": strongest_bear,
        "key_risks": list(dict.fromkeys(key_risks))[:6],
        "key_opportunities": list(dict.fromkeys(key_opportunities))[:6],
        "confidence_level": round(confidence, 2),
        "data_coverage": data_coverage,
        "what_matters_most": what_matters,
        "what_is_noise": what_is_noise,
        "what_investors_may_be_overlooking": overlooked,
        "where_thesis_is_strongest": strongest_bull,
        "where_thesis_is_weakest": strongest_bear,
        "final_stance_body": final_body,
        "evidence": {
            "fundamentals": ((fundamentals or {}).get("sections") or {}).get("earnings_quality", {}).get("items", [])[:3],
            "valuation": (valuation.get("items") or [])[:3],
            "peer_benchmarking": (peer_benchmarking.get("items") or [])[:3],
            "conviction": (reasons_for + reasons_against)[:6],
            "coverage_missing": missing,
            "sector_framework": sector_framework,
            "portfolio": portfolio_context or {},
            "wealth": wealth_context or {},
            "committee": committee_context or {},
            "risk": risk_context or {},
        },
        "generated_at_utc": datetime.utcnow().isoformat(),
    }
