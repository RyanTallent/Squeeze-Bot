from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def _risk_label(score: float) -> str:
    if score >= 75:
        return "Low"
    if score >= 50:
        return "Medium"
    return "High"


def _confidence(evidence_count: int) -> float:
    if evidence_count >= 20:
        return 0.85
    if evidence_count >= 10:
        return 0.70
    if evidence_count >= 5:
        return 0.50
    if evidence_count >= 1:
        return 0.25
    return 0.10


def _latest_research_by_ticker(research_reports: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in sorted(research_reports or [], key=lambda r: str(r.get("created_at_utc") or ""), reverse=True):
        report = row.get("report_json") or {}
        ticker = str(report.get("ticker") or row.get("ticker") or "").upper()
        if ticker and ticker not in latest:
            latest[ticker] = report
    return latest


def enrich_holdings(holdings: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    total_value = 0.0
    total_cost = 0.0
    for h in holdings:
        shares = _num(h.get("shares"))
        avg_cost = _num(h.get("average_cost"))
        current_price = _num(h.get("current_price"), avg_cost)
        market_value = shares * current_price
        cost_basis = shares * avg_cost
        total_value += market_value
        total_cost += cost_basis
        rows.append(
            {
                **h,
                "ticker": (h.get("ticker") or "").upper(),
                "shares": shares,
                "average_cost": avg_cost,
                "current_price": current_price,
                "market_value": market_value,
                "cost_basis": cost_basis,
                "unrealized_pnl": market_value - cost_basis,
                "unrealized_pnl_pct": ((market_value - cost_basis) / cost_basis) if cost_basis else None,
                "realized_pnl": h.get("realized_pnl"),
            }
        )
    for row in rows:
        row["allocation_pct"] = (row["market_value"] / total_value) if total_value else 0
    rows.sort(key=lambda r: r["market_value"], reverse=True)
    return {"holdings": rows, "total_value": total_value, "total_cost": total_cost, "total_unrealized_pnl": total_value - total_cost}


def _group_exposure(holdings: list[dict[str, Any]], key: str, total_value: float) -> list[dict[str, Any]]:
    grouped: dict[str, float] = defaultdict(float)
    for h in holdings:
        grouped[h.get(key) or f"Unknown {key.title()}"] += _num(h.get("market_value"))
    rows = [
        {"name": name, "market_value": value, "allocation_pct": (value / total_value) if total_value else 0}
        for name, value in grouped.items()
    ]
    rows.sort(key=lambda r: r["market_value"], reverse=True)
    return rows


def analyze_portfolio(holdings: list[dict[str, Any]], goals: dict[str, Any] | None = None, research_reports: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    enriched = enrich_holdings(holdings)
    rows = enriched["holdings"]
    total_value = enriched["total_value"]
    research_by_ticker = _latest_research_by_ticker(research_reports)
    for row in rows:
        research = research_by_ticker.get(row["ticker"])
        conviction = (research or {}).get("conviction") or {}
        valuation = (research or {}).get("valuation") or {}
        peer = (research or {}).get("peer_benchmarking") or {}
        row["research"] = {
            "verdict": (research or {}).get("verdict"),
            "conviction_score": conviction.get("score"),
            "conviction_rating": conviction.get("rating"),
            "valuation_rating": valuation.get("rating"),
            "peer_rating": peer.get("rating"),
            "generated_at_utc": (research or {}).get("generated_at_utc"),
        } if research else None
    sector_exposure = _group_exposure(rows, "sector", total_value)
    industry_exposure = _group_exposure(rows, "industry", total_value)
    top_holding = rows[0] if rows else None

    warnings = []
    score = 82
    if top_holding and top_holding["allocation_pct"] >= 0.35:
        score -= 25
        warnings.append(f"{top_holding['ticker']} is oversized at {top_holding['allocation_pct'] * 100:.1f}% of portfolio value.")
    elif top_holding and top_holding["allocation_pct"] >= 0.20:
        score -= 12
        warnings.append(f"{top_holding['ticker']} is a large position at {top_holding['allocation_pct'] * 100:.1f}%.")

    if sector_exposure and sector_exposure[0]["allocation_pct"] >= 0.50:
        score -= 20
        warnings.append(f"Sector concentration: {sector_exposure[0]['name']} is {sector_exposure[0]['allocation_pct'] * 100:.1f}%.")
    elif sector_exposure and sector_exposure[0]["allocation_pct"] >= 0.35:
        score -= 10
        warnings.append(f"Sector overweight: {sector_exposure[0]['name']} is {sector_exposure[0]['allocation_pct'] * 100:.1f}%.")

    if len(rows) <= 2 and total_value > 0:
        score -= 10
        warnings.append("Portfolio has few holdings; single-name risk may be elevated.")

    researched_rows = [r for r in rows if r.get("research") and r["research"].get("conviction_score") is not None]
    weighted_conviction = None
    low_conviction = []
    expensive_exposure = []
    if researched_rows:
        researched_weight = sum(r.get("allocation_pct") or 0 for r in researched_rows)
        weighted_conviction = (
            sum((r.get("allocation_pct") or 0) * _num(r["research"].get("conviction_score")) for r in researched_rows) / researched_weight
            if researched_weight
            else None
        )
        low_conviction = [r for r in researched_rows if _num(r["research"].get("conviction_score")) < 45]
        expensive_exposure = [r for r in researched_rows if r["research"].get("valuation_rating") in ("Expensive", "Very Expensive")]
        if low_conviction:
            score -= min(18, len(low_conviction) * 6)
            warnings.append("One or more holdings have low current research conviction.")
        if expensive_exposure:
            score -= min(12, len(expensive_exposure) * 4)
            warnings.append("One or more researched holdings screen expensive on valuation.")

    score = max(0, min(100, score))
    risk_label = _risk_label(score)
    confidence = _confidence(len(rows))

    opportunities = []
    if not rows:
        opportunities.append("Add holdings to unlock exposure and concentration analysis.")
    if warnings:
        opportunities.append("Review position sizing and concentration before adding similar exposure.")
    else:
        opportunities.append("No major concentration issue detected from current holdings.")
    if researched_rows:
        conviction_label = f"{weighted_conviction:.0f}/100" if weighted_conviction is not None else "n/a"
        opportunities.append(f"Research-aware portfolio layer active for {len(researched_rows)} holding(s). Weighted conviction: {conviction_label}.")
    else:
        opportunities.append("Generate Research reports for holdings to unlock conviction-weighted portfolio analysis.")
    for row in low_conviction[:3]:
        opportunities.append(f"Review {row['ticker']}: low conviction score {row['research'].get('conviction_score')}/100.")
    for row in expensive_exposure[:3]:
        opportunities.append(f"Valuation review: {row['ticker']} screens {row['research'].get('valuation_rating')}.")

    return {
        "ok": True,
        "generated_at_utc": datetime.utcnow().isoformat(),
        "portfolio_value": total_value,
        "total_cost": enriched["total_cost"],
        "total_unrealized_pnl": enriched["total_unrealized_pnl"],
        "holdings": rows,
        "top_holding": top_holding,
        "sector_exposure": sector_exposure,
        "industry_exposure": industry_exposure,
        "theme_exposure": [{"name": "Theme analysis pending", "allocation_pct": None, "market_value": None}],
        "risk": {
            "score": score,
            "label": risk_label,
            "confidence": confidence,
            "warnings": warnings or ["No major concentration warning detected yet."],
        },
        "research_overlay": {
            "enabled": True,
            "researched_holdings": len(researched_rows),
            "weighted_conviction": weighted_conviction,
            "low_conviction_holdings": [
                {"ticker": r["ticker"], "allocation_pct": r["allocation_pct"], "conviction_score": r["research"].get("conviction_score"), "conviction_rating": r["research"].get("conviction_rating")}
                for r in low_conviction
            ],
            "expensive_holdings": [
                {"ticker": r["ticker"], "allocation_pct": r["allocation_pct"], "valuation_rating": r["research"].get("valuation_rating")}
                for r in expensive_exposure
            ],
        },
        "portfolio_fit": {
            "goals": goals or {},
            "summary": "Portfolio Intelligence V2 foundation is active: concentration, exposure, and research conviction can now be evaluated together when holdings have Research reports.",
            "warnings": warnings,
            "opportunities": opportunities,
        },
    }
