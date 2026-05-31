from __future__ import annotations

from datetime import datetime
from typing import Any


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


def _endpoint_ok(bundle: dict[str, Any], key: str) -> bool:
    endpoint = ((bundle or {}).get("endpoints") or {}).get(key) or {}
    return bool(endpoint.get("ok")) and bool(_rows(bundle, key))


def _coverage_rating(score: float) -> str:
    if score >= 85:
        return "Excellent"
    if score >= 70:
        return "Good"
    if score >= 50:
        return "Partial"
    if score >= 30:
        return "Weak"
    return "Poor"


def _check(name: str, ok: bool, weight: int, missing_label: str, detail: str = "") -> dict[str, Any]:
    return {"name": name, "ok": ok, "weight": weight, "missing_label": missing_label, "detail": detail}


def build_data_coverage(
    bundle: dict[str, Any] | None,
    profile: dict[str, Any] | None = None,
    sections: dict[str, Any] | None = None,
    sector_framework: dict[str, Any] | None = None,
) -> dict[str, Any]:
    bundle = bundle or {}
    sections = sections or {}
    chart_count = len((profile or {}).get("chart") or [])
    endpoint_rows = {key: len(_rows(bundle, key)) for key in (bundle.get("endpoints") or {}).keys()}
    valuation = sections.get("valuation_analysis") or {}
    peer = sections.get("peer_benchmarking") or {}

    valuation_metric_count = len([m for m in valuation.get("metrics") or [] if m.get("company") is not None])
    peer_count = len(_rows(bundle, "peer_metrics"))
    checks = [
        _check("Price history", chart_count >= 50, 12, "sufficient price history", f"{chart_count} chart rows"),
        _check("Income statement", _endpoint_ok(bundle, "income_statement"), 10, "income statement"),
        _check("Balance sheet", _endpoint_ok(bundle, "balance_sheet"), 10, "balance sheet"),
        _check("Cash flow", _endpoint_ok(bundle, "cash_flow"), 10, "cash flow"),
        _check("Key metrics", _endpoint_ok(bundle, "key_metrics"), 10, "key metrics"),
        _check("Financial ratios", _endpoint_ok(bundle, "financial_ratios"), 10, "financial ratios"),
        _check("Analyst estimates", _endpoint_ok(bundle, "analyst_estimates"), 8, "analyst estimates"),
        _check("Company earnings", _endpoint_ok(bundle, "earnings_calendar"), 5, "company earnings records"),
        _check("Peer list", _endpoint_ok(bundle, "peers"), 6, "peer list"),
        _check("Peer metrics", peer_count >= 2, 8, "peer metrics", f"{peer_count} peer metric rows"),
        _check("Valuation metrics", valuation_metric_count >= 3, 7, "valuation metrics", f"{valuation_metric_count} valuation metric(s)"),
        _check("Sector framework route", bool((sector_framework or {}).get("key")), 4, "sector framework route", (sector_framework or {}).get("name") or ""),
    ]
    max_score = sum(c["weight"] for c in checks)
    earned = sum(c["weight"] for c in checks if c["ok"])
    score = round((earned / max_score) * 100) if max_score else 0
    missing = [c["missing_label"] for c in checks if not c["ok"]]
    provider_status = {
        key: {
            "ok": bool(value.get("ok")),
            "cached": bool(value.get("cached")),
            "row_count": endpoint_rows.get(key, 0),
            "error": value.get("error"),
        }
        for key, value in (bundle.get("endpoints") or {}).items()
        if isinstance(value, dict)
    }
    return {
        "score": score,
        "rating": _coverage_rating(score),
        "missing_data": missing,
        "checks": checks,
        "provider_status": provider_status,
        "peer_count": peer_count,
        "valuation_metric_count": valuation_metric_count,
        "confidence_ceiling": round(min(0.92, max(0.20, score / 100)), 2),
        "timestamp": datetime.utcnow().isoformat(),
    }
