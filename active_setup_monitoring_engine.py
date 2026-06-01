from __future__ import annotations

from datetime import datetime
from typing import Any
import uuid

from trading_intelligence_engine import setup_quality, evaluate_trade_plan


SETUP_STATES = (
    "Watching",
    "Entry Ready",
    "Entry Triggered",
    "Active",
    "Do Not Chase",
    "Target 1 Hit",
    "Target 2 Hit",
    "Target 3 Hit",
    "Stop Threatened",
    "Invalidated",
    "Closed",
)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        val = float(value)
        return val if val == val else default
    except Exception:
        return default


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def _grade_from_score(score: float) -> str:
    if score >= 92:
        return "A+"
    if score >= 84:
        return "A"
    if score >= 76:
        return "B+"
    if score >= 68:
        return "B"
    if score >= 58:
        return "C"
    return "D"


def _playbook_match(setup_type: str, playbook_stats: dict[str, Any]) -> dict[str, Any]:
    best = None
    for row in playbook_stats.get("by_setup") or []:
        if str(row.get("key") or "").lower() == str(setup_type or "").lower():
            best = row
            break
    if not best:
        return {
            "playbook_match_pct": 50,
            "historical_win_rate": None,
            "average_gain": playbook_stats.get("average_winner_score"),
            "average_loss": playbook_stats.get("average_loser_score"),
            "similar_historical_setups": [],
            "confidence": 0.15,
        }
    win_rate = best.get("win_rate")
    expectancy = _num(best.get("expectancy"))
    match = _clamp(50 + expectancy * 20 + _num(win_rate, 0.5) * 30)
    return {
        "playbook_match_pct": round(match),
        "historical_win_rate": win_rate,
        "average_gain": playbook_stats.get("average_winner_score"),
        "average_loss": playbook_stats.get("average_loser_score"),
        "similar_historical_setups": [best],
        "confidence": best.get("confidence") or 0.2,
    }


def _levels_from_row(row: dict[str, Any]) -> dict[str, float]:
    trigger = _num(row.get("trigger"), _num(row.get("close")))
    stop = _num(row.get("stop"), trigger * 0.9 if trigger else 0)
    risk = max(trigger - stop, trigger * 0.02, 0.0001)
    return {
        "entry_trigger": trigger,
        "stop_level": stop,
        "target_1": _num(row.get("target_1"), trigger + risk * 1.5),
        "target_2": _num(row.get("target_2") or row.get("target"), trigger + risk * 2.25),
        "target_3": _num(row.get("target_3") or row.get("ceiling"), trigger + risk * 3.0),
        "chase_threshold": trigger + risk * 0.75,
    }


def create_monitor_from_setup(row: dict[str, Any], playbook_stats: dict[str, Any] | None = None, trade_plan: dict[str, Any] | None = None, scan_id: str | None = None) -> dict[str, Any]:
    playbook_stats = playbook_stats or {}
    intel = row.get("intelligence") or {}
    setup_type = intel.get("setup_type") or row.get("setup_type") or row.get("subtype") or "Unknown Setup"
    quality = setup_quality(row, playbook_stats)
    levels = _levels_from_row(row)
    plan_quality = evaluate_trade_plan(trade_plan) if trade_plan else None
    playbook = _playbook_match(setup_type, playbook_stats)
    conviction = _num(intel.get("confluence_score"), _num(row.get("confidence"), quality.get("score") or 50))
    state = {
        "current_state": "Watching",
        "previous_state": None,
        "setup_grade": quality.get("grade") or intel.get("setup_grade"),
        "previous_grade": None,
        "conviction": round(conviction),
        "previous_conviction": None,
        "current_price": _num(row.get("close")),
        "relative_volume": _num(row.get("rel_vol")),
        "volume_confirmation": _num(row.get("rel_vol")) >= 2.0,
        "momentum_strength": _num((intel.get("subscores") or {}).get("momentum", {}).get("score"), _num(intel.get("confluence_score"), 50)),
        "risk_reward": (plan_quality or {}).get("risk_reward") or _risk_reward(levels["entry_trigger"], levels["stop_level"], levels["target_2"]),
        "status_summary": "Watching scanner-selected setup from Top 5 active monitoring list.",
        "last_evaluated_at_utc": datetime.utcnow().isoformat(),
    }
    return {
        "id": str(uuid.uuid4()),
        "ticker": (row.get("ticker") or "").upper(),
        "scan_id": scan_id or row.get("scan_id"),
        "setup_type": setup_type,
        "scanner_bucket": row.get("bucket"),
        "setup_grade": state["setup_grade"],
        "conviction": state["conviction"],
        "playbook_match_pct": playbook["playbook_match_pct"],
        "historical_win_rate": playbook["historical_win_rate"],
        "average_gain": playbook["average_gain"],
        "average_loss": playbook["average_loss"],
        "similar_historical_setups": playbook["similar_historical_setups"],
        "levels": levels,
        "current_state": state["current_state"],
        "setup_json": row,
        "state_json": state,
        "trade_plan_id": (trade_plan or {}).get("id"),
        "trade_plan_snapshot": trade_plan or {},
        "status": "ACTIVE",
        "created_at_utc": datetime.utcnow().isoformat(),
        "updated_at_utc": datetime.utcnow().isoformat(),
    }


def _risk_reward(entry: float, stop: float, target: float) -> float:
    risk = max(entry - stop, 0.0001)
    reward = max(target - entry, 0)
    return reward / risk if risk else 0


def _event(monitor: dict[str, Any], event_type: str, old_state: str | None, new_state: str, message: str, evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "monitor_id": monitor.get("id"),
        "ticker": monitor.get("ticker"),
        "event_type": event_type,
        "old_state": old_state,
        "new_state": new_state,
        "message": message,
        "evidence": evidence,
        "created_at_utc": datetime.utcnow().isoformat(),
    }


def _select_state(price: float, levels: dict[str, float], rel_vol: float, rr_now: float, prior_state: str) -> tuple[str, str]:
    trigger = levels.get("entry_trigger") or 0
    stop = levels.get("stop_level") or 0
    chase = levels.get("chase_threshold") or 0
    t1, t2, t3 = levels.get("target_1") or 0, levels.get("target_2") or 0, levels.get("target_3") or 0
    extended_pct = ((price - trigger) / trigger) if trigger else 0
    volume_confirms = rel_vol >= 2.0
    if stop and price <= stop:
        return "Invalidated", f"Price broke stop/invalidation at {stop:.4f}."
    if stop and price <= stop * 1.03:
        return "Stop Threatened", f"Price is within 3% of stop {stop:.4f}."
    if t3 and price >= t3:
        return "Target 3 Hit", f"Price reached target 3 at {t3:.4f}."
    if t2 and price >= t2:
        return "Target 2 Hit", f"Price reached target 2 at {t2:.4f}."
    if t1 and price >= t1:
        return "Target 1 Hit", f"Price reached target 1 at {t1:.4f}."
    if trigger and (price >= chase or extended_pct >= 0.08 or rr_now < 1.0):
        return "Do Not Chase", f"Setup remains valid, but price is {extended_pct * 100:.1f}% above trigger and live reward/risk is {rr_now:.2f}."
    if trigger and price >= trigger and volume_confirms:
        return "Entry Triggered", "Price broke the entry trigger with volume confirmation."
    if trigger and price >= trigger * 0.98 and volume_confirms:
        return "Entry Ready", "Price is near trigger and volume confirms."
    if prior_state == "Active":
        return "Active", "Setup is active and still above invalidation."
    return "Watching", "Setup is still developing."


def evaluate_active_setup(monitor: dict[str, Any], current_row: dict[str, Any] | None = None, market_price: float | None = None, playbook_stats: dict[str, Any] | None = None, trade_plan: dict[str, Any] | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    playbook_stats = playbook_stats or {}
    base_row = dict(monitor.get("setup_json") or {})
    row = {**base_row, **(current_row or {})}
    prior_state_json = monitor.get("state_json") or {}
    levels = monitor.get("levels") or _levels_from_row(row)
    price = _num(market_price, _num(row.get("close"), _num(prior_state_json.get("current_price"))))
    rel_vol = _num(row.get("rel_vol"), _num(prior_state_json.get("relative_volume")))
    quality = setup_quality(row, playbook_stats)
    old_state = prior_state_json.get("current_state") or monitor.get("current_state") or "Watching"
    old_grade = prior_state_json.get("setup_grade") or monitor.get("setup_grade")
    old_conviction = _num(prior_state_json.get("conviction"), _num(monitor.get("conviction"), 50))
    rr_now = _risk_reward(price, levels.get("stop_level") or 0, levels.get("target_2") or 0)
    new_state, summary = _select_state(price, levels, rel_vol, rr_now, old_state)
    conviction = round(_clamp((quality.get("score") or 50) * 0.82 + (10 if rel_vol >= 2 else -5 if rel_vol < 1.1 else 0)))
    grade = _grade_from_score(conviction)
    events: list[dict[str, Any]] = []
    evidence = {"price": price, "relative_volume": rel_vol, "risk_reward_now": rr_now, "grade": grade, "conviction": conviction}
    if new_state != old_state:
        events.append(_event(monitor, "state_change", old_state, new_state, summary, evidence))
    if grade != old_grade:
        direction = "increased" if (old_grade or "D") < grade else "changed"
        events.append(_event(monitor, "grade_change", old_state, new_state, f"Setup grade {direction} from {old_grade or 'n/a'} to {grade}.", evidence))
    if abs(conviction - old_conviction) >= 8:
        event_type = "conviction_increased" if conviction > old_conviction else "conviction_decreased"
        events.append(_event(monitor, event_type, old_state, new_state, f"Conviction changed from {old_conviction:.0f} to {conviction:.0f}.", evidence))
    previous_rel_vol = _num(prior_state_json.get("relative_volume"), rel_vol)
    if previous_rel_vol and rel_vol < previous_rel_vol * 0.75:
        events.append(_event(monitor, "volume_weakened", old_state, new_state, "Relative volume weakened materially.", evidence))
    elif rel_vol >= 2 and previous_rel_vol < 2:
        events.append(_event(monitor, "volume_confirmed", old_state, new_state, "Relative volume now confirms the setup.", evidence))
    plan_quality = evaluate_trade_plan(trade_plan or monitor.get("trade_plan_snapshot") or {}) if (trade_plan or monitor.get("trade_plan_snapshot")) else {}
    plan_status = "plan still valid"
    if new_state == "Invalidated":
        plan_status = "plan invalidated"
    elif new_state in ("Stop Threatened", "Do Not Chase"):
        plan_status = "plan weakening"
    elif conviction > old_conviction + 8 or new_state in ("Entry Ready", "Entry Triggered"):
        plan_status = "plan improving"
    playbook = _playbook_match(row.get("setup_type") or monitor.get("setup_type"), playbook_stats)
    new_state_json = {
        **prior_state_json,
        "previous_state": old_state,
        "current_state": new_state,
        "previous_grade": old_grade,
        "setup_grade": grade,
        "previous_conviction": old_conviction,
        "conviction": conviction,
        "current_price": price,
        "relative_volume": rel_vol,
        "volume_confirmation": rel_vol >= 2,
        "momentum_strength": quality["components"]["scanner_confluence"],
        "risk_reward": rr_now,
        "status_summary": summary,
        "plan_status": plan_status,
        "plan_quality": plan_quality,
        "playbook": playbook,
        "last_evaluated_at_utc": datetime.utcnow().isoformat(),
    }
    updated = {
        **monitor,
        "setup_grade": grade,
        "conviction": conviction,
        "playbook_match_pct": playbook["playbook_match_pct"],
        "historical_win_rate": playbook["historical_win_rate"],
        "average_gain": playbook["average_gain"],
        "average_loss": playbook["average_loss"],
        "similar_historical_setups": playbook["similar_historical_setups"],
        "current_state": new_state,
        "state_json": new_state_json,
        "setup_json": row,
        "status": "CLOSED" if new_state == "Closed" else "ACTIVE",
        "updated_at_utc": datetime.utcnow().isoformat(),
    }
    return updated, events


def trader_summary(monitors: list[dict[str, Any]]) -> dict[str, Any]:
    active = [m for m in monitors if (m.get("status") or "ACTIVE") == "ACTIVE"]
    if not active:
        return {"top_5_active_setups": [], "highest_conviction_setup": None, "best_risk_reward_setup": None, "best_momentum_setup": None, "best_short_squeeze_setup": None, "most_dangerous_setup": None, "most_improved_setup": None, "setup_losing_conviction_fastest": None}
    by_conviction = sorted(active, key=lambda m: _num(m.get("conviction")), reverse=True)
    by_rr = sorted(active, key=lambda m: _num((m.get("state_json") or {}).get("risk_reward")), reverse=True)
    by_momentum = sorted(active, key=lambda m: _num((m.get("state_json") or {}).get("momentum_strength")), reverse=True)
    squeeze = [m for m in active if "squeeze" in str(m.get("setup_type") or "").lower() or str(m.get("scanner_bucket") or "").upper() == "SQUEEZE"]
    dangerous = sorted(active, key=lambda m: (m.get("current_state") not in ("Invalidated", "Do Not Chase", "Stop Threatened"), _num(m.get("conviction"))))
    improved = sorted(active, key=lambda m: _num((m.get("state_json") or {}).get("conviction")) - _num((m.get("state_json") or {}).get("previous_conviction")), reverse=True)
    losing = sorted(active, key=lambda m: _num((m.get("state_json") or {}).get("previous_conviction")) - _num((m.get("state_json") or {}).get("conviction")), reverse=True)
    return {
        "top_5_active_setups": by_conviction[:5],
        "highest_conviction_setup": by_conviction[0],
        "best_risk_reward_setup": by_rr[0],
        "best_momentum_setup": by_momentum[0],
        "best_short_squeeze_setup": (sorted(squeeze, key=lambda m: _num(m.get("conviction")), reverse=True)[0] if squeeze else None),
        "most_dangerous_setup": dangerous[0],
        "most_improved_setup": improved[0],
        "setup_losing_conviction_fastest": losing[0],
    }
