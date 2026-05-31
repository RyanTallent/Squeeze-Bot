from __future__ import annotations

from typing import Any


def build_committee_evidence(committee_runs: list[dict[str, Any]] | None = None, **extra: Any) -> dict[str, Any]:
    return {"domain": "committee", "committee_runs": committee_runs or [], "extra": extra}
