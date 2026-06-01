from __future__ import annotations

from datetime import datetime
from typing import Any


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


def _grade(score: float) -> str:
    if score >= 92:
        return "A+"
    if score >= 84:
        return "A"
    if score >= 72:
        return "B"
    if score >= 58:
        return "C"
    return "D"


def _source_context(plan: dict[str, Any]) -> dict[str, Any]:
    plan_json = plan.get("plan_json") or {}
    if isinstance(plan_json, dict):
        return plan_json.get("source_context") or {}
    return {}


def _setup_type(row: dict[str, Any]) -> str:
    intel = row.get("intelligence") or {}
    return intel.get("setup_type") or row.get("setup_type") or row.get("subtype") or "Unknown Setup"


def setup_quality(row: dict[str, Any], playbook_stats: dict[str, Any] | None = None) -> dict[str, Any]:
    intel = row.get("intelligence") or {}
    subs = intel.get("subscores") or {}
    score = _num(intel.get("confluence_score"), _num(row.get("confidence"), 50))
    playbook_match = 50.0
    setup = _setup_type(row)
    for item in (playbook_stats or {}).get("by_setup") or []:
        if str(item.get("key") or "").lower() == str(setup).lower():
            playbook_match = 50 + _num(item.get("expectancy")) * 22 + _num(item.get("win_rate"), 0.5) * 30
            break
    risk_score = _num((subs.get("risk") or {}).get("score"), 55)
    quality = _clamp(score * 0.58 + playbook_match * 0.18 + risk_score * 0.24)
    strengths = list(intel.get("strengths") or [])
    weaknesses = list(intel.get("weaknesses") or [])
    if playbook_match >= 65:
        strengths.append("Setup aligns with positive playbook evidence.")
    elif playbook_match < 45:
        weaknesses.append("Playbook match is weak or unproven.")
    return {
        "ticker": row.get("ticker"),
        "setup_type": setup,
        "score": round(quality),
        "grade": _grade(quality),
        "why": f"{setup} earns {_grade(quality)} from scanner confluence, risk quality, and playbook alignment.",
        "strongest_factors": strengths[:5] or ["No strong factors identified yet."],
        "weakest_factors": weaknesses[:5] or ["No major weakness detected, but confirmation is still required."],
        "components": {
            "scanner_confluence": round(score),
            "playbook_match": round(_clamp(playbook_match)),
            "risk_quality": round(risk_score),
        },
    }


def evaluate_trade_plan(plan: dict[str, Any]) -> dict[str, Any]:
    trigger = _num(plan.get("trigger_price"))
    stop = _num(plan.get("stop_price"))
    target = _num(plan.get("target_2"), _num(plan.get("target_1")))
    entry_low = _num(plan.get("entry_zone_low"), trigger)
    entry_high = _num(plan.get("entry_zone_high"), trigger)
    rr = _num(plan.get("risk_reward"))
    confidence = _num(plan.get("confidence"), 50)
    conviction = _num(plan.get("conviction"), confidence)
    risk = max(trigger - stop, 0.0001) if trigger and stop else 0.0001
    entry_quality = _clamp(78 - max(0, (entry_high - trigger) / risk) * 22)
    stop_quality = _clamp(72 + (10 if stop and stop < trigger else -20))
    target_quality = _clamp(55 + min(rr, 3) * 14)
    rr_quality = _clamp(40 + min(rr, 3) * 20)
    probability = _clamp(confidence * 0.45 + conviction * 0.35 + rr_quality * 0.20)
    position_size = "Normal" if probability >= 70 and rr >= 1.5 else "Reduced" if probability >= 55 else "Small / avoid"
    return {
        "ticker": plan.get("ticker"),
        "plan_id": plan.get("id"),
        "plan_style": plan.get("plan_style"),
        "entry_quality": round(entry_quality),
        "stop_quality": round(stop_quality),
        "target_quality": round(target_quality),
        "risk_reward": rr,
        "risk_reward_quality": round(rr_quality),
        "position_size": position_size,
        "probability_of_success": round(probability),
        "overall_quality": round(_clamp(entry_quality * 0.22 + stop_quality * 0.18 + target_quality * 0.16 + rr_quality * 0.18 + probability * 0.26)),
        "notes": [
            "Entry quality penalizes chasing beyond trigger.",
            "Stop quality rewards clear invalidation below trigger.",
            "Position size is conservative when probability or reward/risk is weak.",
        ],
    }


def detect_trader_mistakes(row: dict[str, Any] | None = None, plan: dict[str, Any] | None = None, playbook_stats: dict[str, Any] | None = None, journal: dict[str, Any] | None = None, memory: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    row = row or _source_context(plan or {})
    plan = plan or {}
    mistakes = []
    close = _num(row.get("close"))
    vwap = _num(row.get("vwap"))
    move = abs(_num(row.get("move_pct")))
    rr = _num(plan.get("risk_reward"))
    allocation_conf = _num(plan.get("confidence"), _num((row.get("intelligence") or {}).get("confluence_score"), 50))
    if vwap and close > vwap * 1.10:
        mistakes.append({"type": "chasing", "severity": "High", "evidence": "Price is materially above VWAP."})
    if move > 0.25:
        mistakes.append({"type": "FOMO entries", "severity": "High", "evidence": "Move is already extended."})
    if rr and rr < 1.2:
        mistakes.append({"type": "poor risk/reward", "severity": "High", "evidence": f"Risk/reward is {rr:.2f}."})
    if allocation_conf < 55:
        mistakes.append({"type": "oversized positions", "severity": "Medium", "evidence": "Low confidence setup requires reduced size."})
    for m in (journal or {}).get("recurring_mistakes") or []:
        title = str(m.get("title") or "").lower()
        if "chasing" in title or "stop" in title or "loss" in title:
            mistakes.append({"type": "repeated playbook mistakes", "severity": "Medium", "evidence": m.get("description") or m.get("title")})
            break
    for mem in memory or []:
        statement = str(mem.get("statement") or "").lower()
        if "revenge" in statement:
            mistakes.append({"type": "revenge trading", "severity": "High", "evidence": mem.get("statement")})
            break
    return mistakes or [{"type": "no major mistake detected", "severity": "Low", "evidence": "No rule-based behavioral warning triggered."}]


def compare_setups(a: dict[str, Any], b: dict[str, Any], playbook_stats: dict[str, Any] | None = None) -> dict[str, Any]:
    qa = setup_quality(a, playbook_stats)
    qb = setup_quality(b, playbook_stats)
    winner = qa if qa["score"] >= qb["score"] else qb
    loser = qb if winner is qa else qa
    return {
        "winner": winner.get("ticker"),
        "winner_grade": winner.get("grade"),
        "comparison": [qa, qb],
        "why": f"{winner.get('ticker')} ranks higher because its setup quality score ({winner.get('score')}) exceeds {loser.get('ticker')} ({loser.get('score')}).",
    }


def rank_setups(rows: list[dict[str, Any]], plans: list[dict[str, Any]] | None = None, playbook_stats: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    plan_by_ticker = {str(p.get("ticker") or "").upper(): p for p in plans or []}
    ranked = []
    for row in rows:
        q = setup_quality(row, playbook_stats)
        plan = plan_by_ticker.get(str(row.get("ticker") or "").upper())
        plan_quality = evaluate_trade_plan(plan)["overall_quality"] if plan else 55
        conviction = _num((row.get("intelligence") or {}).get("confluence_score"), q["score"])
        rank_score = _clamp(q["score"] * 0.42 + plan_quality * 0.22 + q["components"]["playbook_match"] * 0.18 + conviction * 0.18)
        ranked.append({**q, "rank_score": round(rank_score), "plan_quality": plan_quality})
    ranked.sort(key=lambda r: r["rank_score"], reverse=True)
    for idx, row in enumerate(ranked, 1):
        row["rank"] = idx
    return ranked


def daily_trader_view(rows: list[dict[str, Any]], plans: list[dict[str, Any]] | None = None, playbook_stats: dict[str, Any] | None = None) -> dict[str, Any]:
    ranked = rank_setups(rows, plans, playbook_stats)
    squeeze = [r for r in ranked if "squeeze" in str(r.get("setup_type") or "").lower()]
    momentum = [r for r in ranked if "momentum" in str(r.get("setup_type") or "").lower() or "breakout" in str(r.get("setup_type") or "").lower()]
    dangerous = sorted(ranked, key=lambda r: (r["components"]["risk_quality"], -r["rank_score"]))
    best_rr = sorted(ranked, key=lambda r: (r.get("plan_quality") or 0, r["rank_score"]), reverse=True)
    return {
        "highest_conviction_setup": ranked[0] if ranked else None,
        "best_risk_reward": best_rr[0] if best_rr else None,
        "most_dangerous_setup": dangerous[0] if dangerous else None,
        "best_momentum_setup": momentum[0] if momentum else None,
        "best_short_squeeze_setup": squeeze[0] if squeeze else None,
        "top_5_setups": ranked[:5],
    }


def build_trading_intelligence(
    scanner_rows: list[dict[str, Any]],
    trade_plans: list[dict[str, Any]],
    playbook_stats: dict[str, Any],
    journal: dict[str, Any] | None = None,
    risk: dict[str, Any] | None = None,
    memory: list[dict[str, Any]] | None = None,
    discoveries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    active_rows = scanner_rows[:50]
    ranked = rank_setups(active_rows, trade_plans, playbook_stats)
    plan_evaluations = [evaluate_trade_plan(p) for p in (trade_plans or [])[:20]]
    mistake_flags = [
        {"ticker": row.get("ticker"), "mistakes": detect_trader_mistakes(row=row, playbook_stats=playbook_stats, journal=journal, memory=memory)}
        for row in active_rows[:10]
    ]
    return {
        "ok": True,
        "generated_at_utc": datetime.utcnow().isoformat(),
        "setup_rankings": ranked[:25],
        "top_5_setups": ranked[:5],
        "trade_plan_quality": plan_evaluations,
        "mistake_detection": mistake_flags,
        "daily_trader_view": daily_trader_view(active_rows, trade_plans, playbook_stats),
        "context": {
            "scanner_rows": len(scanner_rows),
            "trade_plans": len(trade_plans or []),
            "playbook_sample_size": playbook_stats.get("sample_size") or 0,
            "journal_confidence": (journal or {}).get("confidence"),
            "risk_label": (risk or {}).get("overall_risk_label"),
            "memory_count": len(memory or []),
            "discovery_count": len(discoveries or []),
        },
    }
