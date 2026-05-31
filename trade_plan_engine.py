from __future__ import annotations

import uuid
from typing import Any


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def _fmt(value: float | None, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


PLAN_CONFIG = {
    "aggressive": {
        "entry_pullback": 0.18,
        "entry_breakout_buffer": 0.16,
        "chase_risk": 0.75,
        "confidence_adjust": -7,
        "description": "Earlier entry with less confirmation. Higher upside capture, higher false-breakout risk.",
    },
    "balanced": {
        "entry_pullback": 0.30,
        "entry_breakout_buffer": 0.10,
        "chase_risk": 0.45,
        "confidence_adjust": 0,
        "description": "Default plan balancing confirmation, reward/risk, and execution quality.",
    },
    "conservative": {
        "entry_pullback": 0.45,
        "entry_breakout_buffer": 0.04,
        "chase_risk": 0.25,
        "confidence_adjust": -4,
        "description": "Waits for stronger confirmation or cleaner reset. Lower chase risk, more missed entries.",
    },
}


def build_trade_plan(scanner_row: dict[str, Any], style: str = "balanced") -> dict[str, Any]:
    style = (style or "balanced").lower()
    config = PLAN_CONFIG.get(style, PLAN_CONFIG["balanced"])

    price = _num(scanner_row.get("close"))
    trigger = _num(scanner_row.get("trigger"), price)
    stop = _num(scanner_row.get("stop"), price * 0.92 if price else 0)
    vwap = _num(scanner_row.get("vwap"), price)
    risk_per_share = max(trigger - stop, trigger * 0.02, 0.0001)

    entry_low = max(stop, trigger - risk_per_share * config["entry_pullback"])
    entry_high = trigger + risk_per_share * config["entry_breakout_buffer"]
    chase_threshold = trigger + risk_per_share * config["chase_risk"]

    target_1 = trigger + risk_per_share * 1.5
    target_2 = trigger + risk_per_share * 2.25
    target_3 = trigger + risk_per_share * 3.0
    rr = (target_2 - trigger) / risk_per_share if risk_per_share else None

    intel = scanner_row.get("intelligence") or {}
    confidence = max(0, min(100, _num(intel.get("confluence_score"), _num(scanner_row.get("confidence"), 50)) + config["confidence_adjust"]))
    conviction_penalty = 10 if "High" in str(intel.get("risk_flag")) else 0
    conviction = max(0, min(100, confidence - conviction_penalty))

    return {
        "id": str(uuid.uuid4()),
        "ticker": scanner_row.get("ticker"),
        "plan_style": style,
        "description": config["description"],
        "setup_type": intel.get("setup_type") or scanner_row.get("subtype"),
        "setup_grade": intel.get("setup_grade"),
        "entry_zone_low": entry_low,
        "entry_zone_high": entry_high,
        "trigger_price": trigger,
        "chase_threshold": chase_threshold,
        "stop_price": stop,
        "target_1": target_1,
        "target_2": target_2,
        "target_3": target_3,
        "risk_reward": rr,
        "confidence": confidence,
        "conviction": conviction,
        "valid_conditions": [
            "Relative volume remains elevated.",
            "Liquidity/spread remain tradable.",
            "Structure holds above invalidation.",
            "Price is not materially beyond chase threshold.",
        ],
        "invalidation_conditions": [
            "Price loses stop/invalidation zone.",
            "Relative volume fades materially.",
            "Structure breaks or reclaim fails.",
            "Reward/risk deteriorates from chasing.",
        ],
        "notes": [
            f"VWAP reference: {_fmt(vwap)}.",
            "Confirm liquidity and spread before entry.",
            "This is a plan framework, not an instruction to buy or sell.",
        ],
    }


def build_trade_plan_set(scanner_row: dict[str, Any]) -> list[dict[str, Any]]:
    return [build_trade_plan(scanner_row, style) for style in ("aggressive", "balanced", "conservative")]


def summarize_trade_plan(plan: dict[str, Any]) -> str:
    return (
        f"{plan.get('ticker')} {str(plan.get('plan_style') or '').title()} Plan: "
        f"entry zone {_fmt(plan.get('entry_zone_low'))}-{_fmt(plan.get('entry_zone_high'))}, "
        f"trigger {_fmt(plan.get('trigger_price'))}, stop {_fmt(plan.get('stop_price'))}, "
        f"targets {_fmt(plan.get('target_1'))} / {_fmt(plan.get('target_2'))} / {_fmt(plan.get('target_3'))}. "
        "Confirm liquidity, spread, and structure before acting."
    )
