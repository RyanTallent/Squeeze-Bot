from __future__ import annotations

from typing import Any


def build_research_evidence(ticker: str | None = None, research_profile: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    return {"domain": "research", "ticker": ticker, "research_profile": research_profile or {}, "extra": extra}
