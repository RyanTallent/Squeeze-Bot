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


def _rating_score(label: str | None, default: float = 50.0) -> float:
    value = str(label or "").lower()
    if value in ("very high conviction", "high", "excellent", "undervalued"):
        return 82
    if value in ("high conviction", "good", "fairly valued"):
        return 68
    if value in ("medium conviction", "medium", "partial"):
        return 52
    if value in ("low conviction", "low", "weak", "expensive"):
        return 35
    if value in ("avoid / watchlist only", "poor", "very expensive"):
        return 18
    return default


def _latest_research_by_ticker(research_reports: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in sorted(research_reports or [], key=lambda r: str(r.get("created_at_utc") or ""), reverse=True):
        report = row.get("report_json") or {}
        profile = row.get("profile_json") or {}
        ticker = str(report.get("ticker") or row.get("ticker") or "").upper()
        if ticker and ticker not in latest:
            latest[ticker] = {"report": report, "profile": profile, "row": row}
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


def _weighted_avg(holdings: list[dict[str, Any]], value_fn) -> float | None:
    weighted_sum = 0.0
    weight_sum = 0.0
    for h in holdings:
        value = value_fn(h)
        if value is None:
            continue
        weight = h.get("allocation_pct") or 0
        weighted_sum += weight * float(value)
        weight_sum += weight
    return weighted_sum / weight_sum if weight_sum else None


def _position_strength(row: dict[str, Any]) -> float:
    research = row.get("research") or {}
    conviction = research.get("conviction_score")
    valuation = _rating_score(research.get("valuation_rating"), 50)
    coverage = research.get("data_coverage_score")
    pnl = row.get("unrealized_pnl_pct")
    score = 50.0
    if conviction is not None:
        score = score * 0.30 + float(conviction) * 0.45
    if valuation is not None:
        score += (valuation - 50) * 0.15
    if coverage is not None:
        score += (float(coverage) - 50) * 0.10
    if pnl is not None:
        score += max(-8, min(8, float(pnl) * 25))
    if (row.get("allocation_pct") or 0) > 0.30 and (conviction or 50) < 60:
        score -= 8
    return max(0, min(100, score))


def _chart_returns(research_entry: dict[str, Any] | None) -> list[float]:
    profile = (research_entry or {}).get("profile") or {}
    chart = profile.get("chart") or []
    closes = [float(p.get("close")) for p in chart if p.get("close") not in (None, "")]
    returns = []
    for idx in range(1, len(closes)):
        prev = closes[idx - 1]
        if prev:
            returns.append((closes[idx] - prev) / prev)
    return returns[-90:]


def _correlation(a: list[float], b: list[float]) -> float | None:
    n = min(len(a), len(b))
    if n < 20:
        return None
    x = a[-n:]
    y = b[-n:]
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    cov = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))
    var_x = sum((v - mean_x) ** 2 for v in x)
    var_y = sum((v - mean_y) ** 2 for v in y)
    if var_x <= 0 or var_y <= 0:
        return None
    return cov / ((var_x * var_y) ** 0.5)


def _build_correlation_analysis(rows: list[dict[str, Any]], research_by_ticker: dict[str, dict[str, Any]]) -> dict[str, Any]:
    pairs = []
    high_pairs = []
    for i, left in enumerate(rows):
        for right in rows[i + 1 :]:
            corr = _correlation(_chart_returns(research_by_ticker.get(left["ticker"])), _chart_returns(research_by_ticker.get(right["ticker"])))
            if corr is None:
                continue
            pair = {
                "pair": [left["ticker"], right["ticker"]],
                "correlation": corr,
                "combined_allocation_pct": (left.get("allocation_pct") or 0) + (right.get("allocation_pct") or 0),
            }
            pairs.append(pair)
            if corr >= 0.75:
                high_pairs.append(pair)
    coverage = len(pairs)
    return {
        "pairs": sorted(pairs, key=lambda p: p["correlation"], reverse=True),
        "high_correlation_pairs": sorted(high_pairs, key=lambda p: p["combined_allocation_pct"], reverse=True),
        "coverage_pair_count": coverage,
        "summary": (
            f"{len(high_pairs)} high-correlation pair(s) detected."
            if high_pairs
            else "No high-correlation pair detected from available Research price histories."
            if pairs
            else "Correlation analysis needs Research price history for at least two holdings."
        ),
    }


def _sector_concentration_analysis(sector_exposure: list[dict[str, Any]], rows: list[dict[str, Any]]) -> dict[str, Any]:
    warnings = []
    for sector in sector_exposure:
        pct = sector.get("allocation_pct") or 0
        if pct >= 0.50:
            warnings.append(f"Critical sector concentration: {sector['name']} is {pct * 100:.1f}% of portfolio.")
        elif pct >= 0.35:
            warnings.append(f"Sector overweight: {sector['name']} is {pct * 100:.1f}% of portfolio.")
    unknown = sum((r.get("allocation_pct") or 0) for r in rows if not r.get("sector"))
    if unknown >= 0.20:
        warnings.append(f"Sector metadata missing for {unknown * 100:.1f}% of portfolio.")
    return {"top_sectors": sector_exposure[:5], "warnings": warnings, "largest_sector": sector_exposure[0] if sector_exposure else None}


def _position_sizing_analysis(rows: list[dict[str, Any]], goals: dict[str, Any] | None = None) -> dict[str, Any]:
    max_position = _num((goals or {}).get("max_position_pct"), 20) / 100
    rows_out = []
    warnings = []
    for row in rows:
        conviction = ((row.get("research") or {}).get("conviction_score"))
        suggested_max = max_position
        if conviction is not None:
            if conviction >= 75:
                suggested_max = min(max_position, 0.18)
            elif conviction >= 60:
                suggested_max = min(max_position, 0.12)
            elif conviction >= 45:
                suggested_max = min(max_position, 0.08)
            else:
                suggested_max = min(max_position, 0.04)
        actual = row.get("allocation_pct") or 0
        status = "Appropriate"
        if actual > suggested_max * 1.25:
            status = "Oversized"
            warnings.append(f"{row['ticker']} appears oversized at {actual * 100:.1f}% vs suggested max {suggested_max * 100:.1f}%.")
        elif actual < suggested_max * 0.35 and conviction is not None and conviction >= 70:
            status = "Underweight high-conviction"
        rows_out.append({"ticker": row["ticker"], "allocation_pct": actual, "suggested_max_pct": suggested_max, "status": status, "conviction_score": conviction})
    return {"positions": rows_out, "warnings": warnings}


def _buy_sell_impact(rows: list[dict[str, Any]]) -> dict[str, Any]:
    impacts = []
    for row in rows:
        research = row.get("research") or {}
        conviction = research.get("conviction_score")
        valuation = research.get("valuation_rating")
        allocation = row.get("allocation_pct") or 0
        add_score = (conviction if conviction is not None else 50) - max(0, (allocation - 0.10) * 120)
        trim_score = max(0, allocation - 0.15) * 140
        if valuation in ("Expensive", "Very Expensive"):
            trim_score += 12 if valuation == "Expensive" else 22
            add_score -= 10 if valuation == "Expensive" else 18
        if conviction is not None and conviction < 45:
            trim_score += 18
        impacts.append(
            {
                "ticker": row["ticker"],
                "buy_impact": round(max(0, min(100, add_score))),
                "trim_impact": round(max(0, min(100, trim_score))),
                "interpretation": "Add candidate" if add_score >= 68 else "Trim/review candidate" if trim_score >= 45 else "Hold/monitor",
            }
        )
    return {
        "best_add_candidates": sorted(impacts, key=lambda x: x["buy_impact"], reverse=True)[:5],
        "best_trim_candidates": sorted(impacts, key=lambda x: x["trim_impact"], reverse=True)[:5],
        "all": impacts,
    }


def _stress_testing(rows: list[dict[str, Any]], sector_exposure: list[dict[str, Any]], correlation: dict[str, Any]) -> dict[str, Any]:
    scenarios = []
    scenarios.append({"name": "Market drawdown -10%", "estimated_impact_pct": -0.10, "notes": ["Applies a simple broad-market shock to all holdings."]})
    if sector_exposure:
        top_sector = sector_exposure[0]
        scenarios.append(
            {
                "name": f"{top_sector['name']} sector shock -15%",
                "estimated_impact_pct": -(top_sector.get("allocation_pct") or 0) * 0.15,
                "notes": [f"Largest sector allocation is {top_sector.get('allocation_pct', 0) * 100:.1f}%."],
            }
        )
    expensive_weight = sum((r.get("allocation_pct") or 0) for r in rows if ((r.get("research") or {}).get("valuation_rating") in ("Expensive", "Very Expensive")))
    scenarios.append({"name": "Expensive holdings derating -20%", "estimated_impact_pct": -expensive_weight * 0.20, "notes": [f"Expensive/very expensive researched exposure: {expensive_weight * 100:.1f}%."]})
    high_corr_weight = max([p.get("combined_allocation_pct") or 0 for p in correlation.get("high_correlation_pairs") or []] or [0])
    if high_corr_weight:
        scenarios.append({"name": "High-correlation pair shock -12%", "estimated_impact_pct": -high_corr_weight * 0.12, "notes": ["Largest correlated pair receives a shared downside shock."]})
    worst = min(scenarios, key=lambda s: s["estimated_impact_pct"]) if scenarios else None
    return {"scenarios": scenarios, "worst_case": worst}


def analyze_portfolio(holdings: list[dict[str, Any]], goals: dict[str, Any] | None = None, research_reports: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    enriched = enrich_holdings(holdings)
    rows = enriched["holdings"]
    total_value = enriched["total_value"]
    research_by_ticker = _latest_research_by_ticker(research_reports)
    for row in rows:
        research_entry = research_by_ticker.get(row["ticker"])
        research = (research_entry or {}).get("report") or {}
        conviction = research.get("conviction") or {}
        valuation = research.get("valuation") or {}
        peer = research.get("peer_benchmarking") or {}
        coverage = research.get("data_coverage") or {}
        framework = research.get("sector_framework") or {}
        coverage_missing = coverage.get("missing_data") or research.get("data_gaps") or []
        row["research"] = {
            "verdict": research.get("verdict"),
            "conviction_score": conviction.get("score"),
            "conviction_rating": conviction.get("rating"),
            "valuation_rating": valuation.get("rating"),
            "valuation_score": valuation.get("score"),
            "peer_rating": peer.get("rating"),
            "peer_score": peer.get("score"),
            "data_coverage_score": coverage.get("score"),
            "data_coverage_rating": coverage.get("rating"),
            "data_coverage_missing": coverage_missing,
            "sector_framework": framework.get("key"),
            "generated_at_utc": research.get("generated_at_utc"),
        } if research else None
        row["position_strength_score"] = _position_strength(row)
    sector_exposure = _group_exposure(rows, "sector", total_value)
    industry_exposure = _group_exposure(rows, "industry", total_value)
    top_holding = rows[0] if rows else None
    strongest_position = max(rows, key=lambda r: r.get("position_strength_score") or 0, default=None)
    weakest_position = min(rows, key=lambda r: r.get("position_strength_score") or 100, default=None)
    highest_conviction_holding = max(
        [r for r in rows if (r.get("research") or {}).get("conviction_score") is not None],
        key=lambda r: (r.get("research") or {}).get("conviction_score") or 0,
        default=None,
    )
    most_overvalued_holding = min(
        [r for r in rows if (r.get("research") or {}).get("valuation_score") is not None],
        key=lambda r: (r.get("research") or {}).get("valuation_score") or 100,
        default=None,
    )
    sector_concentration = _sector_concentration_analysis(sector_exposure, rows)
    position_sizing = _position_sizing_analysis(rows, goals)
    correlation = _build_correlation_analysis(rows, research_by_ticker)
    buy_sell_impact = _buy_sell_impact(rows)
    stress_testing = _stress_testing(rows, sector_exposure, correlation)

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
    warnings.extend(sector_concentration["warnings"])
    warnings.extend(position_sizing["warnings"])
    if correlation.get("high_correlation_pairs"):
        score -= min(12, len(correlation["high_correlation_pairs"]) * 4)
        warnings.append("High-correlation holdings may reduce true diversification.")

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

    portfolio_conviction_score = weighted_conviction
    valuation_quality = _weighted_avg(rows, lambda r: _rating_score((r.get("research") or {}).get("valuation_rating"), None))
    coverage_quality = _weighted_avg(rows, lambda r: (r.get("research") or {}).get("data_coverage_score"))
    concentration_penalty = 0
    if top_holding:
        concentration_penalty += max(0, ((top_holding.get("allocation_pct") or 0) - 0.20) * 100)
    if sector_exposure:
        concentration_penalty += max(0, ((sector_exposure[0].get("allocation_pct") or 0) - 0.35) * 70)
    correlation_penalty = min(15, len(correlation.get("high_correlation_pairs") or []) * 5)
    portfolio_risk_score = max(0, min(100, 100 - concentration_penalty - correlation_penalty - (100 - (coverage_quality or 60)) * 0.10))
    if expensive_exposure:
        portfolio_risk_score -= min(10, len(expensive_exposure) * 3)
    portfolio_risk_score = max(0, min(100, portfolio_risk_score))
    risk_adjusted_portfolio_score = (
        (portfolio_conviction_score or 50) * 0.42
        + portfolio_risk_score * 0.34
        + (valuation_quality or 50) * 0.14
        + (coverage_quality or 50) * 0.10
    )
    if portfolio_conviction_score is not None:
        score = (score * 0.45) + (portfolio_risk_score * 0.35) + (portfolio_conviction_score * 0.20)
    score = max(0, min(100, score))
    risk_label = _risk_label(score)
    confidence = _confidence(len(rows))
    if researched_rows:
        confidence = max(confidence, min(0.85, 0.35 + len(researched_rows) * 0.08))

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
    recommendations = []
    if strongest_position:
        recommendations.append(f"Strongest position: {strongest_position['ticker']} with position strength {strongest_position.get('position_strength_score'):.0f}/100.")
    if weakest_position:
        recommendations.append(f"Weakest position: {weakest_position['ticker']} with position strength {weakest_position.get('position_strength_score'):.0f}/100.")
    for warning in warnings[:4]:
        recommendations.append(f"Risk review: {warning}")
    for candidate in buy_sell_impact.get("best_trim_candidates") or []:
        if candidate.get("trim_impact", 0) >= 45:
            recommendations.append(f"Trim/review candidate: {candidate['ticker']} (trim impact {candidate['trim_impact']}/100).")
    if not recommendations:
        recommendations.append("No urgent portfolio action detected. Continue improving data coverage and research history.")
    diversification_warnings = []
    if len(rows) < 5 and total_value > 0:
        diversification_warnings.append("Portfolio has fewer than five holdings; diversification may be limited.")
    if len(sector_exposure) <= 2 and total_value > 0:
        diversification_warnings.append("Portfolio spans two or fewer sectors.")
    diversification_warnings.extend([f"{p['pair'][0]} and {p['pair'][1]} are highly correlated ({p['correlation']:.2f})." for p in correlation.get("high_correlation_pairs", [])[:3]])

    return {
        "ok": True,
        "generated_at_utc": datetime.utcnow().isoformat(),
        "portfolio_value": total_value,
        "total_cost": enriched["total_cost"],
        "total_unrealized_pnl": enriched["total_unrealized_pnl"],
        "holdings": rows,
        "top_holding": top_holding,
        "strongest_position": strongest_position,
        "weakest_position": weakest_position,
        "most_overvalued_holding": most_overvalued_holding,
        "highest_conviction_holding": highest_conviction_holding,
        "sector_exposure": sector_exposure,
        "industry_exposure": industry_exposure,
        "theme_exposure": [{"name": "Theme analysis pending", "allocation_pct": None, "market_value": None}],
        "sector_concentration_analysis": sector_concentration,
        "position_sizing_analysis": position_sizing,
        "correlation_analysis": correlation,
        "buy_sell_impact_analysis": buy_sell_impact,
        "portfolio_stress_testing": stress_testing,
        "portfolio_conviction_score": portfolio_conviction_score,
        "portfolio_risk_score": round(portfolio_risk_score),
        "risk_adjusted_portfolio_score": round(max(0, min(100, risk_adjusted_portfolio_score))),
        "portfolio_recommendations": recommendations[:10],
        "concentration_warnings": warnings,
        "diversification_warnings": diversification_warnings,
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
            "valuation_quality": valuation_quality,
            "coverage_quality": coverage_quality,
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
            "recommendations": recommendations,
        },
    }
