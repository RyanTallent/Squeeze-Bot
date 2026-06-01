from __future__ import annotations

from datetime import datetime
from typing import Any

from valuation_engine import VALUATION_METRICS, extract_company_valuation_metrics, extract_peer_valuation_metrics


BENCHMARK_METRICS = {
    **VALUATION_METRICS,
    "gross_margin": {"label": "Gross Margin", "lower_is_better": False, "fields": ("grossProfitMargin", "grossProfitRatio")},
    "operating_margin": {"label": "Operating Margin", "lower_is_better": False, "fields": ("operatingProfitMargin", "operatingIncomeRatio")},
    "net_margin": {"label": "Net Margin", "lower_is_better": False, "fields": ("netProfitMargin", "netIncomeRatio")},
    "return_on_equity": {"label": "Return on Equity", "lower_is_better": False, "fields": ("returnOnEquity", "roe")},
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


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:+.1f}%"


def _company_metric_set(bundle: dict[str, Any], profile: dict[str, Any] | None = None) -> dict[str, float | None]:
    metrics = extract_company_valuation_metrics(bundle, profile)
    ratios = (_rows(bundle, "financial_ratios") or [{}])[0]
    income = (_rows(bundle, "income_statement") or [{}])[0]
    for key, config in BENCHMARK_METRICS.items():
        if key not in metrics:
            metrics[key] = _first_num(ratios, income, fields=config["fields"])
    return metrics


def _peer_metric_set(peer_record: dict[str, Any]) -> dict[str, float | None]:
    metrics = extract_peer_valuation_metrics(peer_record)
    ratios = peer_record.get("financial_ratios") or {}
    key_metrics = peer_record.get("key_metrics") or {}
    for key, config in BENCHMARK_METRICS.items():
        if key not in metrics:
            metrics[key] = _first_num(ratios, key_metrics, fields=config["fields"])
    return metrics


def _rank_symbols(metric_values: dict[str, float], lower_is_better: bool) -> dict[str, int]:
    sorted_rows = sorted(metric_values.items(), key=lambda kv: kv[1], reverse=not lower_is_better)
    return {symbol: idx + 1 for idx, (symbol, _) in enumerate(sorted_rows)}


def build_peer_benchmarking(bundle: dict[str, Any], profile: dict[str, Any] | None = None) -> dict[str, Any]:
    ticker = str((bundle or {}).get("ticker") or "").upper()
    peer_records = _rows(bundle, "peer_metrics")
    peer_endpoint = ((bundle or {}).get("endpoints") or {}).get("peer_metrics") or {}
    peer_diagnostics = peer_endpoint.get("diagnostics") or {}
    company_metrics = _company_metric_set(bundle, profile)
    peer_metrics = {str(p.get("symbol") or "").upper(): _peer_metric_set(p) for p in peer_records if p.get("symbol")}

    if not peer_metrics:
        return {
            "rating": "Medium",
            "score": 45,
            "items": ["Peer metrics unavailable or incomplete. Peer confidence is reduced, but the peer framework remains active using framework fallback peer definitions."],
            "peer_averages": {},
            "strongest_metrics": [],
            "weakest_metrics": [],
            "peer_ranking": [],
            "source": "FMP",
            "timestamp": (bundle or {}).get("fetched_at") or datetime.utcnow().isoformat(),
            "confidence": 0.15,
            "data_requirements": ["FMP stock peers", "FMP peer key metrics", "FMP peer ratios"],
            "provider_ok": False,
            "provider_error": None,
            "diagnostics": {
                "reason": "no_peer_metric_rows",
                "fmp_peer_symbols": peer_diagnostics.get("fmp_peer_symbols") or [],
                "fallback_peer_symbols": peer_diagnostics.get("fallback_peer_symbols") or [],
                "used_peer_symbols": peer_diagnostics.get("used_peer_symbols") or [],
            },
            "calculation": {"formula": "Company metrics ranked against peer metric rows.", "inputs": {"peer_count": 0, "confidence_adjustment": "low due missing peer metrics"}},
        }

    comparisons = []
    peer_averages: dict[str, dict[str, Any]] = {}
    ranking_points: dict[str, float] = {ticker: 0.0, **{symbol: 0.0 for symbol in peer_metrics}}
    ranking_counts: dict[str, int] = {ticker: 0, **{symbol: 0 for symbol in peer_metrics}}

    for key, config in BENCHMARK_METRICS.items():
        company_value = company_metrics.get(key)
        values = {symbol: metrics.get(key) for symbol, metrics in peer_metrics.items()}
        peer_values = [v for v in values.values() if v is not None]
        peer_avg = _avg(peer_values)
        peer_averages[key] = {"label": config["label"], "average": peer_avg, "sample_size": len(peer_values)}
        if company_value is None or peer_avg in (None, 0):
            continue

        metric_values = {symbol: value for symbol, value in values.items() if value is not None}
        metric_values[ticker] = company_value
        ranks = _rank_symbols(metric_values, bool(config["lower_is_better"]))
        field_count = len(metric_values)
        for symbol, rank in ranks.items():
            ranking_points[symbol] += field_count - rank + 1
            ranking_counts[symbol] += 1

        raw_delta = (company_value - peer_avg) / abs(peer_avg)
        favorable_delta = -raw_delta if config["lower_is_better"] else raw_delta
        comparisons.append(
            {
                "key": key,
                "label": config["label"],
                "company": company_value,
                "peer_average": peer_avg,
                "relative_delta": raw_delta,
                "favorable_delta": favorable_delta,
                "rank": ranks.get(ticker),
                "rank_out_of": field_count,
                "lower_is_better": config["lower_is_better"],
            }
        )

    comparisons.sort(key=lambda row: row["favorable_delta"], reverse=True)
    strongest = comparisons[:4]
    weakest = sorted(comparisons, key=lambda row: row["favorable_delta"])[:4]
    ranking = []
    for symbol, points in ranking_points.items():
        count = ranking_counts.get(symbol) or 0
        if not count:
            continue
        ranking.append({"symbol": symbol, "score": points / count, "metric_count": count})
    ranking.sort(key=lambda row: row["score"], reverse=True)
    for idx, row in enumerate(ranking, 1):
        row["rank"] = idx

    company_rank = next((r["rank"] for r in ranking if r["symbol"] == ticker), None)
    company_score = next((r["score"] for r in ranking if r["symbol"] == ticker), 0)
    rank_out_of = len(ranking)
    if company_rank is None:
        rating, score = "Medium", 45
    else:
        percentile = 1 - ((company_rank - 1) / max(1, rank_out_of - 1))
        score = round(25 + percentile * 65)
        rating = "High" if score >= 70 else "Medium" if score >= 45 else "Low"

    items = [
        f"Peer set: {', '.join(peer_metrics.keys())}.",
        f"{ticker} peer rank: {company_rank or 'n/a'} of {rank_out_of or 'n/a'} across {len(comparisons)} comparable metric(s).",
    ]
    for row in strongest[:2]:
        items.append(f"Strong metric: {row['label']} is {_pct(row['favorable_delta'])} favorable vs peer average.")
    for row in weakest[:2]:
        items.append(f"Weak metric: {row['label']} is {_pct(row['favorable_delta'])} favorable vs peer average.")

    return {
        "rating": rating,
        "score": score,
        "items": items,
        "peer_averages": peer_averages,
        "strongest_metrics": strongest,
        "weakest_metrics": weakest,
        "peer_ranking": ranking,
        "company_peer_score": company_score,
        "source": "FMP",
        "timestamp": (bundle or {}).get("fetched_at") or datetime.utcnow().isoformat(),
        "confidence": min(0.85, 0.25 + len(comparisons) * 0.06),
        "data_requirements": [] if comparisons else ["FMP peer key metrics", "FMP peer ratios"],
        "provider_ok": bool(comparisons),
        "provider_error": None,
        "diagnostics": {
            "reason": None if comparisons else "peer_rows_available_but_no_comparable_metrics",
            "fmp_peer_symbols": peer_diagnostics.get("fmp_peer_symbols") or [],
            "fallback_peer_symbols": peer_diagnostics.get("fallback_peer_symbols") or [],
            "used_peer_symbols": peer_diagnostics.get("used_peer_symbols") or list(peer_metrics.keys()),
            "peer_metric_symbols": list(peer_metrics.keys()),
            "comparison_count": len(comparisons),
            "missing_metric_symbols": [symbol for symbol, metrics in peer_metrics.items() if not any(v is not None for v in metrics.values())],
        },
        "calculation": {
            "formula": "Rank company and peer metrics; lower valuation multiples are better, profitability metrics are higher-is-better.",
            "inputs": {"peer_count": len(peer_metrics), "metric_count": len(comparisons), "company_rank": company_rank, "rank_out_of": rank_out_of},
            "rating_logic": "Higher peer rank and favorable metric deltas improve the peer benchmarking score.",
        },
    }
