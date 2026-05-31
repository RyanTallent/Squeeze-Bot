from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PraetorContext:
    user: dict[str, Any]
    page: str
    module: str
    scanner_row: dict[str, Any] | None = None
    trade_plan: dict[str, Any] | None = None
    playbook: dict[str, Any] | None = None
    memory: list[dict[str, Any]] = field(default_factory=list)
    portfolio: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "user": {
                "id": self.user.get("id"),
                "email": self.user.get("email"),
                "plan_code": self.user.get("plan_code"),
            },
            "page": self.page,
            "module": self.module,
            "scanner_row": self.scanner_row,
            "trade_plan": self.trade_plan,
            "playbook": self.playbook,
            "memory": self.memory,
            "portfolio": self.portfolio,
            "extra": self.extra,
        }


def build_scanner_context(
    user: dict[str, Any],
    scanner_row: dict[str, Any],
    playbook: dict[str, Any] | None = None,
    memory: list[dict[str, Any]] | None = None,
) -> PraetorContext:
    return PraetorContext(
        user=user,
        page="scanner",
        module="scanner_ai",
        scanner_row=scanner_row,
        playbook=playbook or {},
        memory=memory or [],
    )
