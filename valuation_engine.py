from __future__ import annotations

from datetime import datetime
from typing import Any


VALUATION_METRICS = {
    "pe_ratio": {"label": "P/E", "lower_is_better": True, "fields": ("peRatio", "priceEarningsRatio")},
    "forward_pe": {"label": "Forward P/E", "lower_is_better": True, "fields": ("forwardPE", "forwardPe", "forwardPERatio")},
    "ev_sales": {"label": "EV/Sales", "lower_is_better": True, "fields": ("evToSales", "enterpriseValueOverRevenue", "enterpriseValueToRevenue")},
    "ev_ebitda": {"label": "EV/EBITDA", "lower_is_better": True, "fields": ("enterpriseValueOverEBITDA", "evToEbitda", "enterpriseValueMultiple")},
    "price_sales": {"label": "Price/Sales", "lower_is_better": True, "fields": ("priceToSalesRatio", "priceSalesRatio")},
    "peg": {"label": "PEG", "lower_is_better": True, "fields": ("pegRatio", "priceEarningsToGrowthRatio")},
}


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


def _num(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        val = float(value)
        return val if val == val else None
    except Exception:
        return None


def _first_num(*sources: dict[str, Any], fields: tuple[str, ...]) -> float | None:
    for source in sources:
        for field in fields:
            value = _num((source or {}).get(field))
            if value is not None:
                return value
    return None


def _meta(bundle: dict[str, Any]) -> dict[str, Any]:
    endpoints = (bundle or {}).get("endpoints") or {}
    stamps = [v.get("fetched_at") for v in endpoints.values() if isinstance(v, dict) and v.get("fetched_at")]
    return {
        "source": "FMP",
        "timestamp": stamps[0] if stamps else (bundle or {}).get("fetched_at") or datetime.utcnow().isoformat(),
        "provider_ok": any(bool(v.get("ok")) for v in endpoints.values() if isinstance(v, dict)),
    }


def extract_company_valuation_metrics(bundle: dict[str, Any], profile: dict[str, Any] | None = None) -> dict[str, float | None]:
    key_metrics = (_rows(bundle, "key_metrics") or [{}])[0]
    ratios = (_rows(bundle, "financial_ratios") or [{}])[0]
    estimates = (_rows(bundle, "analyst_estimates") or [{}])[0]
    profile_metrics = (profile or {}).get("metrics") or {}
    latest_price = _num(profile_metrics.get("latest_close") or key_metrics.get("price") or ratios.get("price"))
    estimated_eps = _num(estimates.get("estimatedEpsAvg") or estimates.get("epsAvg") or estimates.get("epsEstimatedAvg"))

    metrics: dict[str, float | None] = {}
    for key, config in VALUATION_METRICS.items():
        metrics[key] = _first_num(key_metrics, ratios, fields=config["fields"])
    if metrics.get("forward_pe") is None and latest_price is not None and estimated_eps and estimated_eps > 0:
        metrics["forward_pe"] = latest_price / estimated_eps
    return metrics


def extract_peer_valuation_metrics(peer_record: dict[str, Any]) -> dict[str, float | None]:
    key_metrics = peer_record.get("key_metrics") or {}
    ratios = peer_record.get("financial_ratios") or {}
    return {key: _first_num(key_metrics, ratios, fields=config["fields"]) for key, config in VALUATION_METRICS.items()}


def _average(values: list[float]) -> float | None:
    return (sum(values) / len(values)) if values else None


def _rating_from_premium(avg_premium: float | None, evidence_count: int) -> tuple[str, float]:
    if evidence_count == 0:
        return "Data Unavailable", 20
    if avg_premium is None:
        return "Fairly Valued", 50
    if avg_premium <= -0.20:
        return "Undervalued", 78
    if avg_premium <= 0.20:
        return "Fairly Valued", 58
    if avg_premium <= 0.65:
        return "Expensive", 35
    return "Very Expensive", 18


def _absolute_metric_signal(key: str, value: float | None) -> float | None:
    if value is None or value <= 0:
        return None
    if key in ("pe_ratio", "forward_pe"):
        if value <= 15:
            return -0.25
        if value <= 25:
            return 0.0
        if value <= 40:
            return 0.35
        return 0.75
    if key == "peg":
        if value <= 1:
            return -0.25
        if value <= 1.8:
            return 0.0
        if value <= 3:
            return 0.35
        return 0.75
    if key in ("ev_sales", "price_sales"):
        if value <= 2:
            return -0.25
        if value <= 5:
            return 0.0
        if value <= 10:
            return 0.35
        return 0.75
    if key == "ev_ebitda":
        if value <= 10:
            return -0.25
        if value <= 16:
            return 0.0
        if value <= 25:
            return 0.35
        return 0.75
    return None


def build_valuation_analysis(
    bundle: dict[str, Any],
    profile: dict[str, Any] | None = None,
    peer_benchmarking: dict[str, Any] | None = None,
) -> dict[str, Any]:
    company = extract_company_valuation_metrics(bundle, profile)
    peer_averages = (peer_benchmarking or {}).get("peer_averages") or {}
    comparisons = []
    premiums = []
    absolute_signals = []

    for key, config in VALUATION_METRICS.items():
        company_value = company.get(key)
        peer_avg = _num((peer_averages.get(key) or {}).get("average") if isinstance(peer_averages.get(key), dict) else peer_averages.get(key))
        premium = None
        if company_value is not None and peer_avg not in (None, 0):
            premium = (company_value - peer_avg) / abs(peer_avg)
            premiums.append(premium)
        elif company_value is not None:
            signal = _absolute_metric_signal(key, company_value)
            if signal is not None:
                absolute_signals.append(signal)
        comparisons.append(
            {
                "key": key,
                "label": config["label"],
                "company": company_value,
                "peer_average": peer_avg,
                "relative_premium": premium,
                "lower_is_better": config["lower_is_better"],
            }
        )

    avg_premium = _average(premiums) if premiums else _average(absolute_signals)
    evidence_count = len(premiums) + len(absolute_signals)
    rating, score = _rating_from_premium(avg_premium, evidence_count)
    meta = _meta(bundle)

    items = []
    for row in comparisons:
        if row["company"] is None:
            items.append(f"{row['label']}: unavailable.")
            continue
        if row["peer_average"] is None:
            items.append(f"{row['label']}: {row['company']:.2f} (peer average unavailable).")
            continue
        premium = row["relative_premium"]
        items.append(f"{row['label']}: {row['company']:.2f} vs peer average {row['peer_average']:.2f} ({premium * 100:+.1f}%).")

    return {
        "rating": rating,
        "score": round(score),
        "items": items or ["Valuation metrics unavailable from FMP."],
        "metrics": comparisons,
        "source": meta["source"],
        "timestamp": meta["timestamp"],
        "confidence": min(0.80, 0.20 + evidence_count * 0.10),
        "data_requirements": [] if evidence_count else ["FMP key metrics", "FMP ratios", "FMP analyst estimates"],
        "provider_ok": meta["provider_ok"],
        "provider_error": None,
        "calculation": {
            "formula": "Average relative premium/discount across valuation multiples versus peers; fallback uses conservative absolute thresholds.",
            "inputs": {"average_relative_premium": avg_premium, "evidence_count": evidence_count},
            "rating_logic": "Lower valuation multiples versus peers improve rating; large premiums become Expensive or Very Expensive.",
        },
    }
