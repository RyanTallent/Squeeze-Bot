from __future__ import annotations

from typing import Any


def build_risk_evidence(risk_report: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    return {"domain": "risk", "risk_report": risk_report or {}, "extra": extra}
