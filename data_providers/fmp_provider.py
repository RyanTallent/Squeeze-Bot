from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any, Callable
from urllib.parse import urlencode

import requests

from ticker_normalization import normalize_ticker_for_provider, ticker_normalization_metadata


FMP_BASE_URL = "https://financialmodelingprep.com"
FMP_API_KEY = (os.getenv("FMP_API_KEY") or "").strip()


CacheGet = Callable[[str], dict[str, Any] | None]
CacheSet = Callable[[str, str, dict[str, Any], datetime], None]


def fmp_status() -> dict[str, Any]:
    return {
        "provider": "financial_modeling_prep",
        "configured": bool(FMP_API_KEY),
        "base_url": FMP_BASE_URL,
    }


class FMPProvider:
    def __init__(self, cache_get: CacheGet | None = None, cache_set: CacheSet | None = None):
        self.api_key = FMP_API_KEY
        self.cache_get = cache_get
        self.cache_set = cache_set

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _cache_key(self, endpoint: str, params: dict[str, Any]) -> str:
        parts = "&".join(f"{k}={params[k]}" for k in sorted(params))
        return f"fmp:{endpoint}?{parts}"

    def _safe_url(self, endpoint: str, params: dict[str, Any]) -> str:
        return f"{FMP_BASE_URL}{endpoint}?{urlencode(params)}"

    @staticmethod
    def _filter_symbol_data(data: Any, symbol: str) -> Any:
        if not isinstance(data, list):
            return data
        filtered = []
        for row in data:
            if not isinstance(row, dict):
                filtered.append(row)
                continue
            row_symbol = normalize_ticker_for_provider(str(row.get("symbol") or row.get("ticker") or ""), "fmp")
            if not row_symbol or row_symbol == normalize_ticker_for_provider(symbol, "fmp"):
                filtered.append(row)
        return filtered

    @staticmethod
    def _rows(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
        if isinstance(data, dict):
            rows = data.get("data") or data.get("results") or data.get("rows")
            if isinstance(rows, list):
                return [r for r in rows if isinstance(r, dict)]
            return [data]
        return []

    @classmethod
    def _peer_symbols(cls, peers_result: dict[str, Any], symbol: str, limit: int = 6) -> list[str]:
        rows = cls._rows(peers_result.get("data"))
        symbols: list[str] = []
        for row in rows:
            raw = row.get("peersList") or row.get("peers") if isinstance(row, dict) else None
            if not raw and isinstance(row, dict):
                raw = [row.get("symbol") or row.get("ticker")]
            if not isinstance(raw, list):
                continue
            for item in raw:
                peer = normalize_ticker_for_provider(str(item or "").upper().strip(), "fmp")
                if peer and peer != symbol and peer not in symbols:
                    symbols.append(peer)
                if len(symbols) >= limit:
                    return symbols
        return symbols

    def get(self, endpoint: str, params: dict[str, Any] | None = None, ttl_hours: int = 24) -> dict[str, Any]:
        params = dict(params or {})
        cache_key = self._cache_key(endpoint, params)
        if self.cache_get:
            cached = self.cache_get(cache_key)
            if cached is not None:
                return {
                    "ok": True,
                    "source": "FMP",
                    "cached": True,
                    "fetched_at": cached.get("fetched_at"),
                    "endpoint": endpoint,
                    "params": params,
                    "url": self._safe_url(endpoint, params),
                    "data": cached.get("data"),
                }

        if not self.api_key:
            return {
                "ok": False,
                "source": "FMP",
                "cached": False,
                "endpoint": endpoint,
                "params": params,
                "url": self._safe_url(endpoint, params),
                "error": "FMP_API_KEY not configured",
                "data": None,
            }

        url = f"{FMP_BASE_URL}{endpoint}"
        request_params = {**params, "apikey": self.api_key}
        try:
            response = requests.get(url, params=request_params, timeout=25)
            if response.status_code >= 400:
                return {
                    "ok": False,
                    "source": "FMP",
                    "cached": False,
                    "endpoint": endpoint,
                    "params": params,
                    "url": self._safe_url(endpoint, params),
                    "error": f"FMP {response.status_code}: {response.text[:200]}",
                    "data": None,
                }
            data = response.json()
            if params.get("symbol"):
                data = self._filter_symbol_data(data, str(params["symbol"]))
        except requests.RequestException as e:
            return {
                "ok": False,
                "source": "FMP",
                "cached": False,
                "endpoint": endpoint,
                "params": params,
                "url": self._safe_url(endpoint, params),
                "error": f"{type(e).__name__}: {str(e)[:200]}",
                "data": None,
            }
        except Exception as e:
            return {
                "ok": False,
                "source": "FMP",
                "cached": False,
                "endpoint": endpoint,
                "params": params,
                "url": self._safe_url(endpoint, params),
                "error": f"Invalid FMP response: {str(e)[:200]}",
                "data": None,
            }

        fetched_at = datetime.utcnow()
        if self.cache_set:
            self.cache_set(cache_key, "FMP", data, fetched_at + timedelta(hours=ttl_hours))
        return {
            "ok": True,
            "source": "FMP",
            "cached": False,
            "fetched_at": fetched_at.isoformat(),
            "endpoint": endpoint,
            "params": params,
            "url": self._safe_url(endpoint, params),
            "data": data,
        }

    def fundamentals_bundle(self, ticker: str) -> dict[str, Any]:
        display_symbol = ticker.upper().strip()
        symbol = normalize_ticker_for_provider(display_symbol, "fmp")
        endpoints = {
            "income_statement": ("/stable/income-statement", {"symbol": symbol, "limit": 8}, 24),
            "balance_sheet": ("/stable/balance-sheet-statement", {"symbol": symbol, "limit": 8}, 24),
            "cash_flow": ("/stable/cash-flow-statement", {"symbol": symbol, "limit": 8}, 24),
            "key_metrics": ("/stable/key-metrics", {"symbol": symbol, "limit": 8}, 24),
            "financial_ratios": ("/stable/ratios", {"symbol": symbol, "limit": 8}, 24),
            "analyst_estimates": ("/stable/analyst-estimates", {"symbol": symbol, "period": "annual", "page": 0, "limit": 8}, 12),
            "peers": ("/stable/stock-peers", {"symbol": symbol}, 168),
            "earnings_calendar": ("/stable/earnings", {"symbol": symbol, "limit": 40}, 12),
        }
        results: dict[str, Any] = {}
        for name, (endpoint, params, ttl) in endpoints.items():
            results[name] = self.get(endpoint, params, ttl_hours=ttl)
        peer_symbols = self._peer_symbols(results.get("peers") or {}, symbol)
        peer_metrics = []
        for peer in peer_symbols:
            key_metrics = self.get("/stable/key-metrics", {"symbol": peer, "limit": 1}, ttl_hours=48)
            ratios = self.get("/stable/ratios", {"symbol": peer, "limit": 1}, ttl_hours=48)
            peer_metrics.append(
                {
                    "symbol": peer,
                    "key_metrics": (self._rows(key_metrics.get("data")) or [{}])[0],
                    "financial_ratios": (self._rows(ratios.get("data")) or [{}])[0],
                    "sources": {
                        "key_metrics": {k: key_metrics.get(k) for k in ("ok", "cached", "error", "url", "fetched_at")},
                        "financial_ratios": {k: ratios.get(k) for k in ("ok", "cached", "error", "url", "fetched_at")},
                    },
                }
            )
        results["peer_metrics"] = {
            "ok": bool(peer_metrics),
            "source": "FMP",
            "cached": all(
                bool((p.get("sources") or {}).get(src, {}).get("cached"))
                for p in peer_metrics
                for src in ("key_metrics", "financial_ratios")
            )
            if peer_metrics
            else False,
            "fetched_at": datetime.utcnow().isoformat(),
            "endpoint": "peer metric enrichment",
            "params": {"symbol": symbol, "peers": peer_symbols, "limit": 1},
            "url": "multiple cached FMP peer metric requests",
            "data": peer_metrics,
        }
        return {
            "ok": any(v.get("ok") for v in results.values()),
            "ticker": display_symbol,
            "provider_symbol": symbol,
            "ticker_normalization": ticker_normalization_metadata(display_symbol),
            "provider": "FMP",
            "configured": self.configured,
            "fetched_at": datetime.utcnow().isoformat(),
            "endpoints": results,
        }

    def debug_bundle(self, ticker: str) -> dict[str, Any]:
        bundle = self.fundamentals_bundle(ticker)
        debug = {}
        for name, result in (bundle.get("endpoints") or {}).items():
            data = result.get("data")
            rows = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
            fields = sorted({str(k) for row in rows[:5] if isinstance(row, dict) for k in row.keys()})
            symbols = sorted({str(row.get("symbol") or row.get("ticker")) for row in rows if isinstance(row, dict) and (row.get("symbol") or row.get("ticker"))})
            debug[name] = {
                "ok": result.get("ok"),
                "cached": result.get("cached"),
                "error": result.get("error"),
                "endpoint": result.get("endpoint"),
                "url": result.get("url"),
                "params": result.get("params"),
                "row_count": len(rows),
                "symbols_seen": symbols[:20],
                "fields_sample": fields,
                "raw_sample": rows[:3],
                "fetched_at": result.get("fetched_at"),
            }
        return {
            "ok": bundle.get("ok"),
            "ticker": bundle.get("ticker"),
            "provider": "FMP",
            "configured": self.configured,
            "fetched_at": bundle.get("fetched_at"),
            "endpoints": debug,
        }
