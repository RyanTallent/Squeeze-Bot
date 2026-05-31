from __future__ import annotations

import re
from typing import Any


PROVIDER_TICKER_OVERRIDES: dict[str, dict[str, str]] = {
    "fmp": {
        "BRK.B": "BRK-B",
        "BRK.A": "BRK-A",
        "BF.B": "BF-B",
        "BF.A": "BF-A",
    },
    "polygon": {},
}


def canonical_ticker(ticker: str) -> str:
    return (ticker or "").upper().strip()


def normalize_ticker_for_provider(ticker: str, provider: str) -> str:
    symbol = canonical_ticker(ticker)
    provider_key = (provider or "").lower().strip()
    overrides = PROVIDER_TICKER_OVERRIDES.get(provider_key) or {}
    if symbol in overrides:
        return overrides[symbol]
    if provider_key == "fmp" and re.match(r"^[A-Z]{1,5}\.[A-Z]$", symbol):
        return symbol.replace(".", "-")
    return symbol


def ticker_normalization_metadata(ticker: str) -> dict[str, Any]:
    canonical = canonical_ticker(ticker)
    providers = {
        "fmp": normalize_ticker_for_provider(canonical, "fmp"),
        "polygon": normalize_ticker_for_provider(canonical, "polygon"),
    }
    return {
        "input": ticker,
        "canonical": canonical,
        "providers": providers,
        "changed": {provider: value for provider, value in providers.items() if value != canonical},
        "is_class_share": bool(re.match(r"^[A-Z]{1,5}\.[A-Z]$", canonical)),
    }
