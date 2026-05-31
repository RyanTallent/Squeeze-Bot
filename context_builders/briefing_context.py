from __future__ import annotations

from typing import Any


def build_briefing_evidence(briefings: list[dict[str, Any]] | None = None, **extra: Any) -> dict[str, Any]:
    return {"domain": "briefing", "briefings": briefings or [], "extra": extra}
