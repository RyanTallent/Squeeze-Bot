from __future__ import annotations

from typing import Any


SECTOR_FRAMEWORKS: dict[str, dict[str, Any]] = {
    "banks": {
        "name": "Bank Framework",
        "implementation_status": "routing_ready",
        "fallback_peers": ["JPM", "BAC", "C", "WFC", "GS"],
        "valuation_metrics": ["P/E", "Forward P/E", "Price/Book", "ROE", "ROA"],
        "quality_metrics": ["Net interest margin", "Efficiency ratio", "Deposit growth", "Credit losses", "CET1 capital"],
        "notes": ["EV/Sales and EV/EBITDA are weak bank valuation proxies and should be de-emphasized."],
    },
    "insurance": {
        "name": "Insurance Framework",
        "implementation_status": "routing_ready",
        "fallback_peers": ["BRK-B", "CB", "TRV", "AIG", "PGR"],
        "valuation_metrics": ["P/E", "Price/Book", "Combined ratio", "Float growth", "ROE"],
        "quality_metrics": ["Underwriting margin", "Reserve adequacy", "Investment income", "Catastrophe exposure"],
        "notes": ["Insurance quality depends on underwriting and float economics, not just generic multiples."],
    },
    "conglomerates": {
        "name": "Conglomerate Framework",
        "implementation_status": "routing_ready",
        "fallback_peers": ["BRK-B", "GE", "HON", "MMM", "ITW"],
        "valuation_metrics": ["Sum-of-the-parts", "Operating earnings", "Book value", "Cash/investments", "Look-through earnings"],
        "quality_metrics": ["Segment profitability", "Capital allocation", "Balance-sheet strength", "Management succession"],
        "notes": ["Generic single-business multiples can mislead for diversified holding companies."],
    },
    "pharma": {
        "name": "Pharma Framework",
        "implementation_status": "routing_ready",
        "fallback_peers": ["ABBV", "MRK", "JNJ", "AMGN", "AZN"],
        "valuation_metrics": ["P/E", "Forward P/E", "EV/Sales", "EV/EBITDA", "PEG"],
        "quality_metrics": ["Pipeline depth", "Patent cliffs", "Drug concentration", "R&D productivity", "Regulatory/catalyst calendar"],
        "notes": ["Revenue concentration and pipeline durability should become first-class metrics."],
    },
    "software": {
        "name": "Software Framework",
        "implementation_status": "routing_ready",
        "fallback_peers": ["MSFT", "NOW", "CRM", "SNOW", "DDOG"],
        "valuation_metrics": ["EV/Sales", "Forward P/E", "FCF yield", "Rule of 40", "PEG"],
        "quality_metrics": ["Revenue growth", "Net retention", "Gross margin", "Operating leverage", "Customer concentration"],
        "notes": ["High-growth software requires growth-adjusted valuation, not absolute sales multiples alone."],
    },
    "semiconductors": {
        "name": "Semiconductor Framework",
        "implementation_status": "routing_ready",
        "fallback_peers": ["AMD", "AVGO", "TSM", "INTC", "MRVL"],
        "valuation_metrics": ["Forward P/E", "PEG", "EV/Sales", "EV/EBITDA", "Gross margin"],
        "quality_metrics": ["Revenue growth", "Gross margin", "Capex cycle", "Inventory cycle", "Customer/platform concentration"],
        "notes": ["Cyclical normalization and growth-adjusted multiples are required before final valuation quality."],
    },
    "general": {
        "name": "General Equity Framework",
        "implementation_status": "active_basic",
        "fallback_peers": [],
        "valuation_metrics": ["P/E", "Forward P/E", "EV/Sales", "EV/EBITDA", "Price/Sales", "PEG"],
        "quality_metrics": ["Revenue growth", "Margins", "Cash flow", "Balance sheet", "Analyst expectations"],
        "notes": ["General framework remains active until a sector-specific model is implemented."],
    },
}


TICKER_FRAMEWORK_OVERRIDES = {
    "JPM": "banks",
    "BRK.B": "conglomerates",
    "BRK-B": "conglomerates",
    "LLY": "pharma",
    "NVO": "pharma",
    "PLTR": "software",
    "NVDA": "semiconductors",
}


def select_sector_framework(profile: dict[str, Any] | None = None, fundamentals: dict[str, Any] | None = None) -> dict[str, Any]:
    ticker = str((profile or {}).get("ticker") or (fundamentals or {}).get("ticker") or "").upper().strip()
    profile_sector = str((profile or {}).get("sector") or ((profile or {}).get("metrics") or {}).get("sector") or "").lower()
    profile_industry = str((profile or {}).get("industry") or ((profile or {}).get("metrics") or {}).get("industry") or "").lower()
    haystack = f"{profile_sector} {profile_industry}"

    key = TICKER_FRAMEWORK_OVERRIDES.get(ticker)
    if not key:
        if "bank" in haystack or "financial services" in haystack:
            key = "banks"
        elif "insurance" in haystack:
            key = "insurance"
        elif "conglomerate" in haystack or "holding" in haystack:
            key = "conglomerates"
        elif "pharma" in haystack or "biotech" in haystack or "drug" in haystack:
            key = "pharma"
        elif "software" in haystack:
            key = "software"
        elif "semiconductor" in haystack or "chip" in haystack:
            key = "semiconductors"
        else:
            key = "general"

    framework = dict(SECTOR_FRAMEWORKS[key])
    framework["key"] = key
    framework["ticker"] = ticker
    framework["routing_reason"] = "ticker_override" if ticker in TICKER_FRAMEWORK_OVERRIDES else "sector_industry_match" if key != "general" else "default_general"
    return framework


def available_sector_frameworks() -> dict[str, dict[str, Any]]:
    return SECTOR_FRAMEWORKS


def fallback_peers_for_ticker(ticker: str, profile: dict[str, Any] | None = None, fundamentals: dict[str, Any] | None = None) -> list[str]:
    framework = select_sector_framework(profile={**(profile or {}), "ticker": ticker}, fundamentals=fundamentals)
    ticker_norm = str(ticker or "").upper().replace(".", "-")
    return [p for p in framework.get("fallback_peers") or [] if p.upper() != ticker_norm]
