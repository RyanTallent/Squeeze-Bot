from __future__ import annotations

from typing import Any


def build_journal_evidence(trades: list[dict[str, Any]] | None = None, journal_report: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    return {"domain": "journal", "trades": trades or [], "journal_report": journal_report or {}, "extra": extra}
