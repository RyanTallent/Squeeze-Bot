from __future__ import annotations

from typing import Any


def build_scanner_evidence(scanner_row: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    return {"domain": "scanner", "scanner_row": scanner_row or {}, "extra": extra}
