from __future__ import annotations

from datetime import datetime
from typing import Any


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def _score_label(score: float) -> str:
    if score >= 75:
        return "High"
    if score >= 50:
        return "Medium"
    return "Low"


def _confidence(evidence_count: int) -> float:
    if evidence_count >= 8:
        return 0.75
    if evidence_count >= 4:
        return 0.55
    if evidence_count >= 1:
        return 0.30
    return 0.15


def _score(name: str, score: float, explanation: str, evidence: list[str], confidence: float | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "score": round(max(0, min(100, score))),
        "label": _score_label(score),
        "explanation": explanation,
        "evidence": evidence,
        "confidence": confidence if confidence is not None else _confidence(len(evidence)),
    }


def build_research_scores(profile: dict[str, Any], deterministic_report: dict[str, Any] | None = None) -> dict[str, Any]:
    m = profile.get("metrics") or {}
    latest_close = _num(m.get("latest_close"))
    sma50 = m.get("sma50")
    sma200 = m.get("sma200")
    period_return = m.get("period_return")
    rel_strength = m.get("relative_strength_vs_spy")
    vol = m.get("annualized_volatility")
    dd = m.get("max_drawdown")

    trend_score = 45
    trend_evidence = []
    if sma50 and latest_close >= sma50:
        trend_score += 18
        trend_evidence.append("Price is above the 50-day moving average.")
    if sma200 and latest_close >= sma200:
        trend_score += 22
        trend_evidence.append("Price is above the 200-day moving average.")
    if period_return is not None and period_return > 0:
        trend_score += 10
        trend_evidence.append(f"Selected-period return is positive ({period_return * 100:.1f}%).")

    momentum_score = 45
    momentum_evidence = []
    if period_return is not None:
        momentum_score += max(-20, min(25, period_return * 80))
        momentum_evidence.append(f"Period return: {period_return * 100:.1f}%.")
    if rel_strength is not None:
        momentum_score += max(-18, min(22, rel_strength * 100))
        momentum_evidence.append(f"Relative strength vs SPY: {rel_strength * 100:.1f}%.")

    liquidity_score = 50
    liquidity_evidence = []
    avg_vol = _num(m.get("avg_volume_20"))
    if avg_vol >= 2_000_000:
        liquidity_score += 25
    elif avg_vol >= 500_000:
        liquidity_score += 12
    elif avg_vol > 0:
        liquidity_score -= 10
    liquidity_evidence.append(f"Average 20-day volume: {avg_vol:,.0f} shares.")

    vol_risk_score = 75
    vol_evidence = []
    if vol is not None:
        vol_evidence.append(f"Annualized volatility: {vol * 100:.1f}%.")
        if vol > 0.9:
            vol_risk_score -= 30
        elif vol > 0.55:
            vol_risk_score -= 15
    if dd is not None:
        vol_evidence.append(f"Max drawdown in range: {dd * 100:.1f}%.")
        if dd < -0.45:
            vol_risk_score -= 20
        elif dd < -0.25:
            vol_risk_score -= 10

    fundamental_placeholder = [
        "Fundamental data provider not yet connected for revenue, cash flow, margins, balance sheet, and guidance.",
        "Connect Financial Modeling Prep, Intrinio, FactSet/Capital IQ, or similar provider for higher-confidence scoring.",
    ]

    thesis_strength = (trend_score * 0.28 + momentum_score * 0.24 + liquidity_score * 0.16 + vol_risk_score * 0.20 + 45 * 0.12)
    portfolio_fit = (liquidity_score * 0.25 + vol_risk_score * 0.35 + trend_score * 0.25 + 50 * 0.15)

    return {
        "trend_quality": _score("Trend Quality", trend_score, "Evaluates price location versus moving averages and selected-period trend.", trend_evidence or ["Trend evidence is limited."]),
        "momentum_quality": _score("Momentum Quality", momentum_score, "Evaluates return profile and relative strength versus SPY.", momentum_evidence or ["Momentum evidence is limited."]),
        "liquidity_quality": _score("Liquidity Quality", liquidity_score, "Evaluates tradability using average volume as a first-pass proxy.", liquidity_evidence),
        "volatility_risk": _score("Volatility Risk", vol_risk_score, "Higher score means cleaner/manageable volatility risk.", vol_evidence or ["Volatility evidence is unavailable."]),
        "fundamental_quality": _score("Fundamental Quality", 45, "Placeholder until fundamental data provider is connected.", fundamental_placeholder, 0.15),
        "valuation_risk": _score("Valuation Risk", 45, "Placeholder until valuation data, earnings, and peer multiples are connected.", fundamental_placeholder, 0.15),
        "thesis_strength": _score("Thesis Strength", thesis_strength, "Composite of trend, momentum, liquidity, volatility risk, and currently limited fundamentals.", ["Technical/market evidence available.", "Fundamental evidence currently limited."]),
        "portfolio_fit": _score("Portfolio Fit", portfolio_fit, "Evaluates whether the idea appears suitable for portfolio inclusion based on liquidity, volatility, and trend context.", ["Portfolio-specific goals and holdings improve this score when connected."]),
    }


def build_framework_sections(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "revenue_business": {
            "rating": "Data Unavailable",
            "items": [
                "Revenue growth trends require fundamentals provider integration.",
                "Business segment analysis requires segment revenue data.",
                "Growth driver analysis requires company filings, earnings transcripts, and segment-level data.",
                "Concentration risk requires customer/product/geography revenue breakdowns.",
            ],
            "data_requirements": ["income statements", "segment revenue", "10-K/10-Q filings", "earnings transcripts"],
        },
        "earnings_quality": {
            "rating": "Medium",
            "items": [
                "Earnings consistency unavailable until earnings history is connected.",
                "Cash flow support unavailable until cash-flow statements are connected.",
                "Margin stability unavailable until financial statements are connected.",
                "Quality flags are currently limited to market/technical evidence.",
            ],
            "data_requirements": ["EPS history", "cash-flow statements", "accruals", "margin history"],
        },
        "margin_analysis": {
            "rating": "Data Unavailable",
            "items": [
                "Gross margin trend requires income statement data.",
                "Operating margin trend requires operating income/revenue.",
                "Net margin trend requires net income/revenue.",
            ],
            "data_requirements": ["gross margin", "operating margin", "net margin", "multi-period financials"],
        },
        "balance_sheet": {
            "rating": "Data Unavailable",
            "items": [
                "Debt, liquidity, and leverage require balance sheet data.",
                "Balance-sheet risk cannot be scored confidently yet.",
            ],
            "data_requirements": ["cash", "debt", "current assets/liabilities", "shareholder equity"],
        },
        "sector_benchmarking": {
            "rating": "Data Unavailable",
            "items": [
                "Sector comparison requires sector-level benchmark data.",
                "Peer comparison requires peer universe and valuation/fundamental data.",
            ],
            "data_requirements": ["sector ETF/benchmark", "peer list", "valuation multiples", "growth/margin comparables"],
        },
    }


def build_scenarios(profile: dict[str, Any]) -> list[dict[str, Any]]:
    m = profile.get("metrics") or {}
    price = _num(m.get("latest_close"))
    vol = _num(m.get("annualized_volatility"), 0.4)
    spread = max(0.08, min(0.35, vol / 3))
    return [
        {
            "name": "Bull Case",
            "probability": 0.25,
            "price_range": [price * (1 + spread * 0.7), price * (1 + spread * 1.4)],
            "assumptions": ["Trend remains constructive.", "Relative strength persists.", "Market confirms risk-on behavior."],
            "confirmation": "Price holds above key moving averages with volume support.",
            "invalidation": "Break below intermediate trend with deteriorating volume/relative strength.",
        },
        {
            "name": "Base Case",
            "probability": 0.50,
            "price_range": [price * (1 - spread * 0.5), price * (1 + spread * 0.6)],
            "assumptions": ["Current trend continues without major fundamental surprise.", "Volatility remains within recent range."],
            "confirmation": "Sideways-to-up behavior while holding major support.",
            "invalidation": "Material drawdown or negative catalyst changes thesis quality.",
        },
        {
            "name": "Bear Case",
            "probability": 0.25,
            "price_range": [price * (1 - spread * 1.3), price * (1 - spread * 0.6)],
            "assumptions": ["Trend fails.", "Risk appetite weakens.", "Negative company/sector catalyst appears."],
            "confirmation": "Lower highs/lower lows and breakdown below moving averages.",
            "invalidation": "Reclaim of trend with strong relative strength.",
        },
    ]


def build_institutional_research(profile: dict[str, Any], deterministic_report: dict[str, Any], objective: str = "") -> dict[str, Any]:
    scores = build_research_scores(profile, deterministic_report)
    frameworks = build_framework_sections(profile)
    scenarios = build_scenarios(profile)
    ticker = profile.get("ticker")
    score_values = [v["score"] for v in scores.values()]
    aggregate = sum(score_values) / len(score_values) if score_values else 0
    verdict = "Strong Thesis" if aggregate >= 75 else "Constructive but Risky" if aggregate >= 60 else "Mixed / Needs Confirmation" if aggregate >= 45 else "High Risk / Avoid Chasing"

    return {
        "ok": True,
        "ticker": ticker,
        "objective": objective,
        "generated_at_utc": datetime.utcnow().isoformat(),
        "verdict": verdict,
        "aggregate_score": round(aggregate),
        "scores": scores,
        "frameworks": frameworks,
        "scenarios": scenarios,
        "sections": {
            "executive_view": {
                "title": "Executive View",
                "body": f"{ticker} receives a {verdict} label from the current deterministic research stack. Technical evidence is available; fundamental evidence is limited until provider integrations are added.",
            },
            "institutional_view": {
                "title": "Institutional View",
                "body": "Current institutional view combines trend, momentum, liquidity, volatility, and placeholder fundamental/valuation modules. Missing fundamental data should lower confidence.",
            },
            "bull_case": {
                "title": "Bull Case",
                "body": "Bull case depends on trend persistence, relative strength, liquidity support, and future confirmation from fundamentals/catalysts.",
            },
            "bear_case": {
                "title": "Bear Case",
                "body": "Bear case centers on trend failure, drawdown risk, volatility expansion, and missing fundamental confirmation.",
            },
            "risks": {
                "title": "Risks",
                "body": "Key risks include volatility, drawdown, incomplete financial statement data, unknown valuation context, and missing peer benchmark data.",
            },
            "portfolio_fit": {
                "title": "Portfolio Fit",
                "body": "Portfolio fit is preliminary. It improves when user holdings, goals, risk tolerance, and concentration data are connected.",
            },
            "what_praetor_would_do": {
                "title": "What Praetor Would Do",
                "body": "Treat this as a research candidate, not a prediction. Verify fundamentals, catalyst quality, valuation, and portfolio fit before increasing conviction.",
            },
        },
        "data_gaps": [
            "fundamental statements",
            "valuation multiples",
            "analyst estimates",
            "peer benchmarking",
            "segment revenue",
            "earnings quality metrics",
            "balance sheet details",
        ],
    }
