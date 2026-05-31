from __future__ import annotations

from datetime import datetime
from typing import Any

from data_coverage_engine import build_data_coverage
from peer_benchmarking_engine import build_peer_benchmarking
from sector_frameworks import select_sector_framework
from valuation_engine import build_valuation_analysis


def _rows(bundle: dict[str, Any], key: str) -> list[dict[str, Any]]:
    endpoint = ((bundle or {}).get("endpoints") or {}).get(key) or {}
    data = endpoint.get("data")
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        rows = data.get("data") or data.get("results") or data.get("rows")
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)]
        return [data]
    return []


def _meta(bundle: dict[str, Any], key: str) -> dict[str, Any]:
    endpoint = ((bundle or {}).get("endpoints") or {}).get(key) or {}
    return {
        "source": "FMP",
        "ok": bool(endpoint.get("ok")),
        "cached": bool(endpoint.get("cached")),
        "timestamp": endpoint.get("fetched_at") or bundle.get("fetched_at") or datetime.utcnow().isoformat(),
        "error": endpoint.get("error"),
    }


def _num(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def _growth(new: float | None, old: float | None) -> float | None:
    if new is None or old in (None, 0):
        return None
    return (new - old) / abs(old)


def _rating_from_score(score: float) -> str:
    if score >= 70:
        return "High"
    if score >= 45:
        return "Medium"
    return "Low"


def _section(
    rating: str,
    items: list[str],
    bundle: dict[str, Any],
    key: str,
    confidence: float,
    data_requirements: list[str] | None = None,
    calculation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = _meta(bundle, key)
    return {
        "rating": rating,
        "items": items,
        "source": meta["source"],
        "timestamp": meta["timestamp"],
        "confidence": confidence,
        "data_requirements": data_requirements or [],
        "provider_ok": meta["ok"],
        "provider_error": meta["error"],
        "calculation": calculation or {},
    }


def revenue_analysis(bundle: dict[str, Any]) -> dict[str, Any]:
    rows = _rows(bundle, "income_statement")
    if len(rows) < 2:
        return _section(
            "Data Unavailable",
            ["Revenue growth trends require at least two income statement periods from FMP."],
            bundle,
            "income_statement",
            0.15,
            ["FMP income statement"],
        )
    latest, prev = rows[0], rows[1]
    rev_latest = _num(latest.get("revenue"))
    rev_prev = _num(prev.get("revenue"))
    growth = _growth(rev_latest, rev_prev)
    score = 50 + ((growth or 0) * 120)
    rating = _rating_from_score(max(0, min(100, score)))
    items = [
        f"Latest revenue: ${rev_latest:,.0f}" if rev_latest is not None else "Latest revenue unavailable.",
        f"Prior period revenue: ${rev_prev:,.0f}" if rev_prev is not None else "Prior period revenue unavailable.",
        f"Revenue growth: {_pct(growth)}",
        "Business segment analysis requires segment-level data; FMP statement data does not always include segment breakdowns.",
        "Growth driver analysis requires filings, earnings transcripts, and management commentary.",
    ]
    return _section(
        rating,
        items,
        bundle,
        "income_statement",
        0.65 if growth is not None else 0.35,
        ["segment revenue", "filings/transcripts for drivers"],
        {
            "formula": "(latest revenue - prior revenue) / prior revenue",
            "inputs": {"latest_revenue": rev_latest, "prior_revenue": rev_prev, "growth": growth},
            "rating_logic": "Revenue growth improves rating; missing or declining revenue lowers confidence/rating.",
        },
    )


def earnings_quality(bundle: dict[str, Any]) -> dict[str, Any]:
    income = _rows(bundle, "income_statement")
    cashflow = _rows(bundle, "cash_flow")
    if not income or not cashflow:
        return _section("Data Unavailable", ["Earnings quality requires income statement and cash-flow data from FMP."], bundle, "cash_flow", 0.15, ["FMP income statement", "FMP cash flow"])
    latest_i = income[0]
    latest_cf = cashflow[0]
    net_income = _num(latest_i.get("netIncome"))
    operating_cf = _num(latest_cf.get("operatingCashFlow"))
    free_cf = _num(latest_cf.get("freeCashFlow"))
    score = 50
    items = []
    if net_income is not None:
        items.append(f"Net income: ${net_income:,.0f}")
    if operating_cf is not None:
        items.append(f"Operating cash flow: ${operating_cf:,.0f}")
    if free_cf is not None:
        items.append(f"Free cash flow: ${free_cf:,.0f}")
    if net_income and operating_cf is not None:
        if operating_cf >= net_income:
            score += 20
            items.append("Operating cash flow supports reported net income.")
        else:
            score -= 20
            items.append("Operating cash flow is below reported net income; quality deserves review.")
    if free_cf is not None and free_cf > 0:
        score += 10
        items.append("Free cash flow is positive.")
    elif free_cf is not None:
        score -= 10
        items.append("Free cash flow is negative.")
    return _section(
        _rating_from_score(score),
        items or ["Earnings quality evidence limited."],
        bundle,
        "cash_flow",
        0.65,
        calculation={
            "formula": "cash-flow support + free cash flow positivity",
            "inputs": {"net_income": net_income, "operating_cash_flow": operating_cf, "free_cash_flow": free_cf, "score": score},
            "rating_logic": "Operating cash flow supporting net income and positive free cash flow improve rating.",
        },
    )


def margin_analysis(bundle: dict[str, Any]) -> dict[str, Any]:
    ratios = _rows(bundle, "financial_ratios")
    income = _rows(bundle, "income_statement")
    if not ratios and not income:
        return _section("Data Unavailable", ["Margin analysis requires FMP ratios or income statements."], bundle, "financial_ratios", 0.15, ["FMP ratios", "FMP income statement"])
    latest = ratios[0] if ratios else {}
    latest_i = income[0] if income else {}
    gross = _num(latest.get("grossProfitMargin") or latest_i.get("grossProfitRatio"))
    operating = _num(latest.get("operatingProfitMargin") or latest_i.get("operatingIncomeRatio"))
    net = _num(latest.get("netProfitMargin") or latest_i.get("netIncomeRatio"))
    score = 50
    for margin in (gross, operating, net):
        if margin is not None:
            score += max(-15, min(15, margin * 40))
    items = [f"Gross margin: {_pct(gross)}", f"Operating margin: {_pct(operating)}", f"Net margin: {_pct(net)}"]
    if len(ratios) >= 2:
        prev = ratios[1]
        prev_net = _num(prev.get("netProfitMargin"))
        trend = _growth(net, prev_net)
        items.append(f"Net margin trend vs prior period: {_pct(trend)}")
        if trend is not None and trend > 0:
            score += 8
        elif trend is not None and trend < -0.05:
            score -= 8
    return _section(
        _rating_from_score(score),
        items,
        bundle,
        "financial_ratios",
        0.60,
        calculation={
            "formula": "gross/operating/net margin level + net margin trend",
            "inputs": {"gross_margin": gross, "operating_margin": operating, "net_margin": net, "score": score},
            "rating_logic": "Higher/stable margins improve rating; deterioration lowers score.",
        },
    )


def balance_sheet_analysis(bundle: dict[str, Any]) -> dict[str, Any]:
    rows = _rows(bundle, "balance_sheet")
    ratios = _rows(bundle, "financial_ratios")
    if not rows:
        return _section("Data Unavailable", ["Balance sheet analysis requires FMP balance sheet data."], bundle, "balance_sheet", 0.15, ["FMP balance sheet"])
    latest = rows[0]
    cash = _num(latest.get("cashAndCashEquivalents"))
    debt = _num(latest.get("totalDebt"))
    assets = _num(latest.get("totalAssets"))
    liabilities = _num(latest.get("totalLiabilities"))
    current_ratio = _num((ratios[0] if ratios else {}).get("currentRatio"))
    score = 65
    items = [
        f"Cash: ${cash:,.0f}" if cash is not None else "Cash unavailable.",
        f"Total debt: ${debt:,.0f}" if debt is not None else "Debt unavailable.",
        f"Assets: ${assets:,.0f}" if assets is not None else "Assets unavailable.",
        f"Liabilities: ${liabilities:,.0f}" if liabilities is not None else "Liabilities unavailable.",
        f"Current ratio: {current_ratio:.2f}" if current_ratio is not None else "Current ratio unavailable.",
    ]
    if debt is not None and cash is not None:
        if cash >= debt:
            score += 12
            items.append("Cash covers total debt.")
        elif debt > cash * 3:
            score -= 18
            items.append("Debt materially exceeds cash.")
    if current_ratio is not None:
        if current_ratio >= 1.5:
            score += 8
        elif current_ratio < 1:
            score -= 12
            items.append("Current ratio below 1.0 indicates liquidity pressure.")
    risk_rating = "Low" if score >= 70 else "Medium" if score >= 45 else "High"
    return _section(
        risk_rating,
        items,
        bundle,
        "balance_sheet",
        0.65,
        calculation={
            "formula": "cash vs debt + current ratio + assets/liabilities context",
            "inputs": {"cash": cash, "debt": debt, "assets": assets, "liabilities": liabilities, "current_ratio": current_ratio, "score": score},
            "rating_logic": "Balance-sheet rating is risk-oriented: Low risk is better than High risk.",
        },
    )


def analyst_expectations(bundle: dict[str, Any]) -> dict[str, Any]:
    rows = _rows(bundle, "analyst_estimates")
    earnings = _rows(bundle, "earnings_calendar")
    if not rows and not earnings:
        return _section("Data Unavailable", ["Analyst expectations require FMP analyst estimates or earnings calendar data."], bundle, "analyst_estimates", 0.15, ["FMP analyst estimates", "FMP earnings calendar"])
    latest = rows[0] if rows else {}
    eps = _num(latest.get("estimatedEpsAvg") or latest.get("epsAvg") or latest.get("epsEstimatedAvg"))
    revenue = _num(latest.get("estimatedRevenueAvg") or latest.get("revenueAvg") or latest.get("revenueEstimatedAvg"))
    items = [
        f"Estimated EPS avg: {eps:.2f}" if eps is not None else "Estimated EPS unavailable.",
        f"Estimated revenue avg: ${revenue:,.0f}" if revenue is not None else "Estimated revenue unavailable.",
    ]
    if earnings:
        items.append(f"Company earnings records available: {len(earnings)}")
    rating = "High" if eps is not None and revenue is not None else "Medium"
    return _section(
        rating,
        items,
        bundle,
        "analyst_estimates",
        0.55 if eps is not None or revenue is not None else 0.30,
        calculation={
            "formula": "presence of EPS/revenue consensus estimates + company earnings records",
            "inputs": {"estimated_eps_avg": eps, "estimated_revenue_avg": revenue, "earnings_record_count": len(earnings)},
            "rating_logic": "Both EPS and revenue estimates available = High. Partial estimates = Medium.",
        },
    )


def sector_benchmarking(bundle: dict[str, Any]) -> dict[str, Any]:
    peers = _rows(bundle, "peers")
    metrics = _rows(bundle, "key_metrics")
    items = []
    if peers:
        peer_list = peers[0].get("peersList") or peers[0].get("peers") or peers
        if isinstance(peer_list, list):
            items.append("Peers: " + ", ".join(str(x) for x in peer_list[:10]))
    if metrics:
        latest = metrics[0]
        pe = _num(latest.get("peRatio"))
        ev_sales = _num(latest.get("evToSales"))
        if pe is not None:
            items.append(f"P/E ratio: {pe:.2f}")
        if ev_sales is not None:
            items.append(f"EV/Sales: {ev_sales:.2f}")
    if not items:
        return _section("Data Unavailable", ["Peer/benchmark data unavailable from FMP response."], bundle, "peers", 0.15, ["FMP stock peers", "FMP key metrics"])
    return _section(
        "Medium",
        items,
        bundle,
        "peers",
        0.45,
        calculation={
            "formula": "peer list + key valuation metrics availability",
            "inputs": {"peer_count": len(peers), "key_metric_records": len(metrics)},
            "rating_logic": "Peer list and valuation metrics enable baseline benchmarking; full peer comparison requires fetching peer metrics.",
        },
    )


def build_fundamental_analysis(bundle: dict[str, Any], profile: dict[str, Any] | None = None) -> dict[str, Any]:
    sector_framework = select_sector_framework(profile=profile, fundamentals=bundle)
    peer_benchmarking = build_peer_benchmarking(bundle, profile=profile)
    valuation = build_valuation_analysis(bundle, profile=profile, peer_benchmarking=peer_benchmarking)
    sections = {
        "revenue_business": revenue_analysis(bundle),
        "earnings_quality": earnings_quality(bundle),
        "margin_analysis": margin_analysis(bundle),
        "balance_sheet": balance_sheet_analysis(bundle),
        "sector_benchmarking": sector_benchmarking(bundle),
        "peer_benchmarking": peer_benchmarking,
        "valuation_analysis": valuation,
        "analyst_expectations": analyst_expectations(bundle),
    }
    data_coverage = build_data_coverage(bundle, profile=profile, sections=sections, sector_framework=sector_framework)
    return {
        "ok": bool(bundle.get("ok")),
        "provider": "FMP",
        "ticker": bundle.get("ticker"),
        "provider_symbol": bundle.get("provider_symbol"),
        "ticker_normalization": bundle.get("ticker_normalization") or {},
        "fetched_at": bundle.get("fetched_at"),
        "sector_framework": sector_framework,
        "data_coverage": data_coverage,
        "sections": sections,
        "raw_endpoints": bundle.get("endpoints") or {},
    }
