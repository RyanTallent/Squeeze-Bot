from __future__ import annotations

from typing import Any


def build_portfolio_evidence(holdings: list[dict[str, Any]] | None = None, portfolio_summary: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    return {"domain": "portfolio", "holdings": holdings or [], "portfolio_summary": portfolio_summary or {}, "extra": extra}
