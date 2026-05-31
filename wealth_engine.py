from __future__ import annotations

from datetime import datetime
from typing import Any


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        val = float(value)
        return val if val == val else default
    except Exception:
        return default


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _rating(score: float) -> str:
    if score >= 80:
        return "Excellent"
    if score >= 65:
        return "Good"
    if score >= 50:
        return "Fair"
    if score >= 35:
        return "Weak"
    return "Poor"


def _latest_research_reports(research_reports: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in sorted(research_reports or [], key=lambda r: str(r.get("created_at_utc") or ""), reverse=True):
        report = row.get("report_json") or {}
        ticker = str(report.get("ticker") or row.get("ticker") or "").upper()
        if ticker and ticker not in latest:
            latest[ticker] = report
    return latest


def _research_score(report: dict[str, Any]) -> float:
    conviction = (report.get("conviction") or {}).get("score")
    valuation = (report.get("valuation") or {}).get("score")
    peer = (report.get("peer_benchmarking") or {}).get("score")
    coverage = (report.get("data_coverage") or {}).get("score")
    return (
        _num(conviction, 50) * 0.48
        + _num(valuation, 50) * 0.20
        + _num(peer, 50) * 0.16
        + _num(coverage, 50) * 0.16
    )


def _confidence_from_coverage(coverage: float, evidence_count: int) -> float:
    return round(min(0.90, max(0.20, coverage / 100) * 0.75 + min(0.15, evidence_count * 0.025)), 2)


def _holding_action(row: dict[str, Any]) -> dict[str, Any]:
    research = row.get("research") or {}
    conviction = research.get("conviction_score")
    valuation_score = research.get("valuation_score")
    valuation_rating = research.get("valuation_rating")
    coverage = research.get("data_coverage_score")
    coverage_missing = research.get("data_coverage_missing") or []
    allocation = row.get("allocation_pct") or 0
    strength = row.get("position_strength_score")
    evidence = []
    missing = []

    if conviction is None:
        missing.append("Research conviction")
    else:
        evidence.append(f"Conviction score: {conviction}/100 ({research.get('conviction_rating') or 'n/a'}).")
    if valuation_score is None:
        missing.append("Valuation score")
    else:
        evidence.append(f"Valuation: {valuation_rating or 'n/a'} ({valuation_score}/100).")
    if coverage is None:
        missing.append("Data coverage")
    else:
        evidence.append(f"Data coverage: {coverage}/100 ({research.get('data_coverage_rating') or 'n/a'}).")
        if coverage < 50:
            missing.append("Low data coverage")
            missing.extend(str(item) for item in coverage_missing)
    if strength is not None:
        evidence.append(f"Position strength: {strength:.0f}/100.")
    evidence.append(f"Current allocation: {allocation * 100:.1f}%.")

    score = _num(conviction, 45) * 0.45 + _num(valuation_score, 45) * 0.22 + _num(coverage, 45) * 0.13 + _num(strength, 45) * 0.20
    if allocation >= 0.30:
        score -= 14
        evidence.append("Large existing allocation reduces add attractiveness.")
    elif allocation >= 0.20:
        score -= 7
        evidence.append("Position is already a large allocation.")
    if valuation_rating == "Very Expensive":
        score -= 10
    elif valuation_rating == "Expensive":
        score -= 5

    if missing:
        if allocation >= 0.25:
            action = "Reduce"
            reasoning = "Recommendation is conservative because required research evidence is incomplete and the position is large."
        else:
            action = "Hold" if allocation > 0 else "Avoid"
            reasoning = "Recommendation is conservative because required research evidence is incomplete."
    elif score >= 72 and allocation < 0.18:
        action = "Buy"
        reasoning = "Research quality, conviction, and sizing leave room for incremental capital."
    elif score >= 52:
        action = "Hold"
        reasoning = "Evidence supports maintaining exposure while monitoring valuation, coverage, and concentration."
    elif score >= 36:
        action = "Reduce"
        reasoning = "Evidence is mixed or risk-adjusted quality is weak relative to the current allocation."
    else:
        action = "Avoid"
        reasoning = "Connected research and portfolio evidence do not support adding capital."

    return {
        "ticker": row.get("ticker"),
        "action": action,
        "score": round(_clamp(score)),
        "reasoning": reasoning,
        "confidence": _confidence_from_coverage(_num(coverage, 30), len(evidence)),
        "supporting_evidence": evidence,
        "missing_data": missing,
        "current_allocation_pct": allocation,
        "market_value": row.get("market_value"),
    }


def _opportunity_from_report(report: dict[str, Any], holding_allocations: dict[str, float]) -> dict[str, Any]:
    ticker = str(report.get("ticker") or "").upper()
    conviction = report.get("conviction") or {}
    valuation = report.get("valuation") or {}
    peer = report.get("peer_benchmarking") or {}
    coverage = report.get("data_coverage") or {}
    score = _research_score(report)
    existing_allocation = holding_allocations.get(ticker, 0)
    if existing_allocation >= 0.20:
        score -= 8
    evidence = [
        f"Conviction: {conviction.get('score', 'n/a')}/100 ({conviction.get('rating', 'n/a')}).",
        f"Valuation: {valuation.get('rating', 'n/a')} ({valuation.get('score', 'n/a')}/100).",
        f"Peer benchmarking: {peer.get('rating', 'n/a')} ({peer.get('score', 'n/a')}/100).",
        f"Data coverage: {coverage.get('score', 'n/a')}/100 ({coverage.get('rating', 'n/a')}).",
    ]
    missing = coverage.get("missing_data") or report.get("data_gaps") or []
    if missing:
        score -= min(18, len(missing) * 3)
    return {
        "ticker": ticker,
        "score": round(_clamp(score)),
        "existing_allocation_pct": existing_allocation,
        "recommendation": "Candidate for cash deployment" if score >= 65 and existing_allocation < 0.20 else "Watchlist / wait",
        "confidence": _confidence_from_coverage(_num(coverage.get("score"), 35), len(evidence)),
        "supporting_evidence": evidence,
        "missing_data": missing,
        "verdict": report.get("verdict"),
    }


def _cash_recommendations(available_cash: float, portfolio: dict[str, Any], research_reports: list[dict[str, Any]] | None) -> dict[str, Any]:
    holdings = portfolio.get("holdings") or []
    holding_allocations = {h.get("ticker"): h.get("allocation_pct") or 0 for h in holdings}
    reports = _latest_research_reports(research_reports)
    opportunities = [_opportunity_from_report(report, holding_allocations) for report in reports.values()]
    opportunities.sort(key=lambda x: x["score"], reverse=True)
    risk_score = portfolio.get("portfolio_risk_score")
    concentration_warnings = portfolio.get("concentration_warnings") or []
    if available_cash <= 0:
        best_use = "No available cash entered. Add available capital to generate deployment sizing."
    elif not holdings and opportunities and opportunities[0]["score"] >= 65:
        best_use = f"Starting from cash, deploy gradually toward the highest risk-adjusted opportunity: {opportunities[0]['ticker']}."
    elif risk_score is not None and risk_score < 55:
        best_use = "Hold cash or use it to reduce concentration before adding new risk."
    elif concentration_warnings:
        best_use = "Prioritize diversification; avoid adding to already concentrated exposures."
    elif opportunities and opportunities[0]["score"] >= 65:
        best_use = f"Deploy gradually toward the highest risk-adjusted opportunity: {opportunities[0]['ticker']}."
    else:
        best_use = "Keep cash reserved until higher-conviction, better-covered opportunities are available."
    return {
        "available_cash": available_cash,
        "best_use_of_capital": best_use,
        "highest_conviction_opportunities": opportunities[:5],
        "risk_adjusted_opportunities": [o for o in opportunities if o["score"] >= 55][:5],
        "cash_deployment_notes": [
            "No financial guarantees are provided.",
            "Use staged deployment and keep position-size limits aligned with portfolio risk.",
            "Missing research data lowers confidence and should slow deployment.",
        ],
    }


def _portfolio_scores(portfolio: dict[str, Any]) -> dict[str, Any]:
    health = _num(portfolio.get("risk_adjusted_portfolio_score"), 50)
    conviction = _num(portfolio.get("portfolio_conviction_score"), 50)
    risk = _num(portfolio.get("portfolio_risk_score"), 50)
    holdings = portfolio.get("holdings") or []
    coverage_quality = ((portfolio.get("research_overlay") or {}).get("coverage_quality"))
    if not holdings:
        return {
            "health_score": 35,
            "health_rating": "Weak",
            "diversification_score": 0,
            "diversification_rating": "Poor",
            "conviction_score": 50,
            "conviction_rating": "Fair",
            "concentration_score": 0,
            "concentration_rating": "Poor",
            "portfolio_risk_score": 50,
            "risk_rating": "Fair",
        }
    if coverage_quality is not None:
        coverage_multiplier = 0.50 + (_clamp(_num(coverage_quality), 0, 100) / 200)
        conviction = conviction * coverage_multiplier
    sector_count = len(portfolio.get("sector_exposure") or [])
    top_alloc = max([h.get("allocation_pct") or 0 for h in holdings] or [0])
    top_sector = max([s.get("allocation_pct") or 0 for s in portfolio.get("sector_exposure") or []] or [0])
    diversification = _clamp(35 + min(35, sector_count * 9) + (20 if len(holdings) >= 5 else len(holdings) * 3) - max(0, (top_sector - 0.35) * 80))
    concentration = _clamp(100 - max(0, (top_alloc - 0.15) * 180) - max(0, (top_sector - 0.30) * 130))
    return {
        "health_score": round(_clamp(health)),
        "health_rating": _rating(health),
        "diversification_score": round(diversification),
        "diversification_rating": _rating(diversification),
        "conviction_score": round(_clamp(conviction)),
        "conviction_rating": _rating(conviction),
        "concentration_score": round(concentration),
        "concentration_rating": _rating(concentration),
        "portfolio_risk_score": round(_clamp(risk)),
        "risk_rating": _rating(risk),
    }


def build_wealth_plan(
    portfolio: dict[str, Any],
    research_reports: list[dict[str, Any]] | None = None,
    available_cash: float = 0,
    objective: str = "",
) -> dict[str, Any]:
    holdings = portfolio.get("holdings") or []
    holding_recommendations = [_holding_action(row) for row in holdings]
    cash = _cash_recommendations(_num(available_cash), portfolio, research_reports)
    scores = _portfolio_scores(portfolio)
    missing = []
    if not holdings:
        missing.append("portfolio holdings")
    if not research_reports:
        missing.append("saved Research reports")
    if not any(h.get("research") for h in holdings):
        missing.append("holding-level Research overlays")
    for rec in holding_recommendations:
        for item in rec.get("missing_data") or []:
            label = f"{rec.get('ticker')}: {item}"
            if label not in missing:
                missing.append(label)
    for opportunity in cash.get("highest_conviction_opportunities") or []:
        for item in opportunity.get("missing_data") or []:
            label = f"{opportunity.get('ticker')}: {item}"
            if label not in missing:
                missing.append(label)
    recommendations = []
    for rec in holding_recommendations:
        recommendations.append(f"{rec['ticker']}: {rec['action']} - {rec['reasoning']}")
    if cash["available_cash"] > 0:
        recommendations.append(f"Cash: {cash['best_use_of_capital']}")
    recommendations.extend(portfolio.get("portfolio_recommendations") or [])
    evidence = [
        f"Portfolio risk-adjusted score: {portfolio.get('risk_adjusted_portfolio_score', 'n/a')}/100.",
        f"Portfolio conviction score: {portfolio.get('portfolio_conviction_score', 'n/a') if portfolio.get('portfolio_conviction_score') is not None else 'n/a'}.",
        f"Portfolio risk score: {portfolio.get('portfolio_risk_score', 'n/a')}/100.",
        f"Research reports available: {len(research_reports or [])}.",
    ]
    avg_confidence = (
        sum(r["confidence"] for r in holding_recommendations) / len(holding_recommendations)
        if holding_recommendations
        else 0.25
    )
    if missing:
        avg_confidence = min(avg_confidence, 0.45)
    return {
        "ok": True,
        "generated_at_utc": datetime.utcnow().isoformat(),
        "objective": objective,
        "disclaimer": "Praetor provides evidence-based analysis, not financial guarantees. Outcomes are uncertain and all recommendations require user review.",
        "scores": scores,
        "holding_recommendations": holding_recommendations,
        "cash_recommendations": cash,
        "portfolio_recommendations": recommendations[:12],
        "supporting_evidence": evidence,
        "missing_data": missing,
        "confidence": round(avg_confidence, 2),
        "source_of_truth": ["Research reports", "Peer Benchmarking", "Valuation", "Conviction", "Data Coverage", "Portfolio Intelligence V2", "Committee/Briefing context"],
    }
