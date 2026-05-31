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


def analyze_portfolio(holdings: list[dict[str, Any]], goals: dict[str, Any] | None = None) -> dict[str, Any]:
    enriched = enrich_holdings(holdings)
    rows = enriched["holdings"]
    total_value = enriched["total_value"]
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
        "portfolio_fit": {
            "goals": goals or {},
            "summary": "Portfolio fit foundation is active. Goal alignment will improve as user goals and suitability profile mature.",
            "warnings": warnings,
            "opportunities": opportunities,
        },
    }
