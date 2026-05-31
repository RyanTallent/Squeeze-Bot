from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def _outcome_value(outcome: str | None) -> int | None:
    out = (outcome or "").lower()
    if out == "winner":
        return 1
    if out == "loser":
        return -1
    if out == "break_even":
        return 0
    return None


def _confidence(sample_size: int) -> float:
    if sample_size <= 0:
        return 0.0
    if sample_size >= 30:
        return 0.90
    if sample_size >= 15:
        return 0.75
    if sample_size >= 8:
        return 0.55
    if sample_size >= 3:
        return 0.35
    return 0.18


def _time_bucket(created_at: str | None) -> str:
    if not created_at:
        return "Unknown Time"
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        hour = dt.hour
    except Exception:
        return "Unknown Time"
    if 8 <= hour < 11:
        return "Morning"
    if 11 <= hour < 15:
        return "Midday"
    if 15 <= hour < 20:
        return "Afternoon / Power Hour"
    return "Extended Hours"


def _source_context(plan: dict[str, Any]) -> dict[str, Any]:
    pj = plan.get("plan_json") or {}
    if isinstance(pj, dict):
        return pj.get("source_context") or {}
    return {}


def _sector(plan: dict[str, Any]) -> str:
    return _source_context(plan).get("sector") or "Unknown Sector"


def _market_condition(plan: dict[str, Any]) -> str:
    ctx = _source_context(plan)
    return ctx.get("session_status") or ctx.get("trend_alignment") or plan.get("status") or "Unknown Condition"


def _group_stats(plans: list[dict[str, Any]], key_fn) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in plans:
        grouped[str(key_fn(p) or "Unknown")].append(p)

    rows = []
    for key, items in grouped.items():
        values = [_outcome_value(p.get("outcome")) for p in items]
        values = [v for v in values if v is not None]
        if not values:
            continue
        wins = sum(1 for v in values if v > 0)
        losses = sum(1 for v in values if v < 0)
        breakeven = sum(1 for v in values if v == 0)
        sample = len(values)
        win_rate = wins / sample if sample else None
        expectancy = (wins - losses) / sample if sample else None
        rows.append(
            {
                "key": key,
                "sample_size": sample,
                "wins": wins,
                "losses": losses,
                "break_even": breakeven,
                "win_rate": win_rate,
                "expectancy": expectancy,
                "confidence": _confidence(sample),
            }
        )
    rows.sort(key=lambda r: (r["expectancy"] or -999, r["win_rate"] or 0, r["sample_size"]), reverse=True)
    return rows


def calculate_playbook_stats(plans: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [p for p in plans if _outcome_value(p.get("outcome")) is not None]
    values = [_outcome_value(p.get("outcome")) for p in completed]
    wins = sum(1 for v in values if v and v > 0)
    losses = sum(1 for v in values if v and v < 0)
    breakeven = sum(1 for v in values if v == 0)
    sample = len(completed)

    winners = [p for p in completed if _outcome_value(p.get("outcome")) == 1]
    losers = [p for p in completed if _outcome_value(p.get("outcome")) == -1]
    avg_winner = sum(_num(p.get("confidence")) for p in winners) / len(winners) if winners else None
    avg_loser = sum(_num(p.get("confidence")) for p in losers) / len(losers) if losers else None

    by_setup = _group_stats(completed, lambda p: p.get("setup_type") or "Unknown Setup")
    by_sector = _group_stats(completed, _sector)
    by_time = _group_stats(completed, lambda p: _time_bucket(p.get("created_at_utc")))
    by_market_condition = _group_stats(completed, _market_condition)

    strengths = []
    weaknesses = []
    if by_setup and by_setup[0]["sample_size"] >= 1:
        strengths.append(
            {
                "title": f"Best setup: {by_setup[0]['key']}",
                "description": f"Win rate {by_setup[0]['win_rate'] * 100:.0f}% across {by_setup[0]['sample_size']} completed plan(s).",
                "confidence": by_setup[0]["confidence"],
                "evidence_count": by_setup[0]["sample_size"],
            }
        )
    weak_setups = sorted(by_setup, key=lambda r: (r["expectancy"] or 999, r["win_rate"] or 1))
    if weak_setups and weak_setups[0]["sample_size"] >= 1:
        weaknesses.append(
            {
                "title": f"Weakest setup: {weak_setups[0]['key']}",
                "description": f"Win rate {weak_setups[0]['win_rate'] * 100:.0f}% across {weak_setups[0]['sample_size']} completed plan(s).",
                "confidence": weak_setups[0]["confidence"],
                "evidence_count": weak_setups[0]["sample_size"],
            }
        )

    return {
        "sample_size": sample,
        "wins": wins,
        "losses": losses,
        "break_even": breakeven,
        "win_rate": wins / sample if sample else None,
        "average_winner_score": avg_winner,
        "average_loser_score": avg_loser,
        "expectancy": (wins - losses) / sample if sample else None,
        "by_setup": by_setup,
        "by_sector": by_sector,
        "by_time_of_day": by_time,
        "by_market_condition": by_market_condition,
        "best_setups": by_setup[:3],
        "worst_setups": weak_setups[:3],
        "best_market_conditions": by_market_condition[:3],
        "worst_market_conditions": sorted(by_market_condition, key=lambda r: (r["expectancy"] or 999, r["win_rate"] or 1))[:3],
        "strengths": strengths,
        "weaknesses": weaknesses,
        "generated_at_utc": datetime.utcnow().isoformat(),
    }
