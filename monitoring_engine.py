from __future__ import annotations

from typing import Any


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def _event(
    plan: dict[str, Any],
    event_type: str,
    current_price: float,
    level_price: float,
    message: str,
    why: str,
    why_it_matters: str,
    category: str | None = None,
) -> dict[str, Any]:
    level = level_price or 0.0
    distance_pct = ((current_price - level) / level) if level else None
    return {
        "event_type": event_type,
        "category": category or event_type,
        "ticker": plan.get("ticker"),
        "plan_id": plan.get("id"),
        "related_entity_id": plan.get("id"),
        "related_entity_type": "trade_plan",
        "plan_style": plan.get("plan_style"),
        "setup_type": plan.get("setup_type"),
        "current_price": current_price,
        "level_price": level_price,
        "distance_pct": distance_pct,
        "message": message,
        "why": why,
        "why_it_matters": why_it_matters,
        "what_changed": f"Price {current_price:.4f} reached or crossed monitored level {level_price:.4f}.",
    }


def evaluate_trade_plan(plan: dict[str, Any], current_price: float) -> list[dict[str, Any]]:
    status = (plan.get("status") or "").upper()
    if status not in ("ACTIVE", "TRIGGERED"):
        return []

    events: list[dict[str, Any]] = []
    ticker = plan.get("ticker")
    trigger = _num(plan.get("trigger_price"))
    chase = _num(plan.get("chase_threshold"))
    stop = _num(plan.get("stop_price"))
    targets = [
        ("Target 3 Reached", _num(plan.get("target_3"))),
        ("Target 2 Reached", _num(plan.get("target_2"))),
        ("Target 1 Reached", _num(plan.get("target_1"))),
    ]

    if stop is not None and current_price <= stop:
        events.append(
            _event(
                plan,
                "Stop Warning",
                current_price,
                stop,
                f"{ticker} reached stop/invalidation area.",
                "Price reached or broke the saved stop/invalidation level.",
                "The original plan may no longer be valid and capital protection becomes the priority.",
                category="Stop Warning",
            )
        )
        return events

    for label, level in targets:
        if level is not None and current_price >= level:
            events.append(
                _event(
                    plan,
                    "Target Reached",
                    current_price,
                    level,
                    f"{ticker} reached {label.replace(' Reached', '')}.",
                    f"Price reached {label.replace(' Reached', '')} from the saved plan.",
                    "Targets are plan-management levels. Momentum, liquidity, and user rules should determine next action.",
                    category="Target Reached",
                )
            )
            return events

    if chase is not None and current_price >= chase and status == "ACTIVE":
        events.append(
            _event(
                plan,
                "Risk Alert",
                current_price,
                chase,
                f"{ticker} exceeded chase threshold.",
                "Price moved beyond the saved chase threshold before the plan was marked traded.",
                "Reward/risk may have deteriorated; waiting for reset may be cleaner than chasing.",
                category="Risk Alert",
            )
        )
        return events

    if trigger is not None and current_price >= trigger and status == "ACTIVE":
        events.append(
            _event(
                plan,
                "Entry Trigger",
                current_price,
                trigger,
                f"{ticker} entry trigger reached.",
                "Price reached the saved trigger level.",
                "The setup should be re-checked for liquidity, spread, volume, structure, and risk/reward before entry.",
                category="Entry Trigger",
            )
        )

    return events


def monitor_trade_plans(plans: list[dict[str, Any]], market_prices: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    normalized_prices = {str(k).upper(): _num(v) for k, v in (market_prices or {}).items()}
    for plan in plans:
        ticker = str(plan.get("ticker") or "").upper()
        price = normalized_prices.get(ticker)
        if price is None:
            plan_json = plan.get("plan_json") or {}
            source = plan_json.get("source_context") if isinstance(plan_json, dict) else {}
            price = _num((source or {}).get("close"))
        if price is None:
            continue
        events.extend(evaluate_trade_plan(plan, price))
    return events
