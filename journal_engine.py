from __future__ import annotations

from datetime import datetime
from typing import Any


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def _confidence(sample_size: int, base: float = 0.20) -> float:
    return max(0.0, min(0.95, base + sample_size * 0.06))


def _confidence_label(confidence: float) -> str:
    if confidence >= 0.75:
        return "high"
    if confidence >= 0.40:
        return "medium"
    return "low"


def _closed_trades(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [t for t in trades if t.get("entry_price") is not None and t.get("exit_price") is not None and t.get("shares") is not None]


def _score_from_rate(rate: float | None, fallback: float = 50) -> float:
    if rate is None:
        return fallback
    return max(0, min(100, rate * 100))


def _match_plan_for_trade(trade: dict[str, Any], plans: list[dict[str, Any]]) -> dict[str, Any] | None:
    ticker = (trade.get("ticker") or "").upper()
    date = trade.get("scan_date_ct")
    matches = [p for p in plans if (p.get("ticker") or "").upper() == ticker]
    if date:
        dated = [p for p in matches if str(p.get("created_at_utc") or "").startswith(str(date))]
        if dated:
            return dated[0]
    return matches[0] if matches else None


def review_trade(trade: dict[str, Any], plans: list[dict[str, Any]]) -> dict[str, Any]:
    pnl = _num(trade.get("pnl_dollars"))
    pnl_pct = _num(trade.get("pnl_pct"))
    entry = _num(trade.get("entry_price"))
    exit_px = _num(trade.get("exit_price"))
    plan = _match_plan_for_trade(trade, plans)
    strengths: list[str] = []
    mistakes: list[str] = []
    lessons: list[str] = []

    if pnl > 0:
        strengths.append("Trade closed profitably.")
        lessons.append("Review whether the exit followed the original target or captured momentum efficiently.")
    elif pnl < 0:
        mistakes.append("Trade closed at a loss.")
        lessons.append("Review whether invalidation was respected and whether the entry was too extended.")
    else:
        lessons.append("Break-even outcome; review whether the setup lacked follow-through or execution was defensive.")

    if pnl_pct > 0.08:
        strengths.append("Strong percentage gain captured.")
    if pnl_pct < -0.05:
        mistakes.append("Loss exceeded 5%; check stop discipline and sizing.")

    if plan:
        stop = _num(plan.get("stop_price"))
        target_1 = _num(plan.get("target_1"))
        trigger = _num(plan.get("trigger_price"))
        if trigger and entry > trigger * 1.03:
            mistakes.append("Entry appears above trigger; possible chasing.")
        if stop and exit_px < stop and pnl < 0:
            mistakes.append("Exit occurred below planned stop/invalidation.")
        if target_1 and exit_px >= target_1 and pnl > 0:
            strengths.append("Exit reached or exceeded Target 1.")
        lessons.append(f"Compare execution against saved {plan.get('plan_style') or 'trade'} plan.")
    else:
        mistakes.append("No linked trade plan found for this trade.")
        lessons.append("Save a Praetor trade plan before entry to improve plan-vs-execution review.")

    execution_score = 70 + (15 if pnl > 0 else -15 if pnl < 0 else 0)
    if any("chasing" in m.lower() for m in mistakes):
        execution_score -= 12
    if any("Target 1" in s for s in strengths):
        execution_score += 8
    execution_score = max(0, min(100, execution_score))

    discipline_score = 72
    if any("below planned stop" in m.lower() for m in mistakes):
        discipline_score -= 22
    if plan:
        discipline_score += 6
    else:
        discipline_score -= 10
    discipline_score = max(0, min(100, discipline_score))

    process_score = 65
    if trade.get("review_text"):
        process_score += 10
    if trade.get("plan"):
        process_score += 8
    if not plan:
        process_score -= 8
    process_score = max(0, min(100, process_score))

    return {
        "trade_id": trade.get("id"),
        "ticker": trade.get("ticker"),
        "pnl_dollars": pnl,
        "pnl_pct": pnl_pct,
        "linked_plan_id": plan.get("id") if plan else None,
        "execution_score": execution_score,
        "discipline_score": discipline_score,
        "process_score": process_score,
        "strengths": strengths,
        "mistakes": mistakes,
        "lessons": lessons,
    }


def build_journal_report(
    trades: list[dict[str, Any]],
    plans: list[dict[str, Any]],
    playbook_stats: dict[str, Any],
    memory_items: list[dict[str, Any]],
    discoveries: list[dict[str, Any]],
    risk_report: dict[str, Any],
) -> dict[str, Any]:
    closed = _closed_trades(trades)
    reviews = [review_trade(t, plans) for t in closed[-25:]]
    sample = len(reviews)

    avg_execution = sum(r["execution_score"] for r in reviews) / sample if sample else None
    avg_discipline = sum(r["discipline_score"] for r in reviews) / sample if sample else None
    avg_process = sum(r["process_score"] for r in reviews) / sample if sample else None

    recurring_mistakes: dict[str, int] = {}
    recurring_strengths: dict[str, int] = {}
    for r in reviews:
        for m in r["mistakes"]:
            recurring_mistakes[m] = recurring_mistakes.get(m, 0) + 1
        for s in r["strengths"]:
            recurring_strengths[s] = recurring_strengths.get(s, 0) + 1

    mistakes = sorted(recurring_mistakes.items(), key=lambda x: x[1], reverse=True)
    strengths = sorted(recurring_strengths.items(), key=lambda x: x[1], reverse=True)

    behavioral_observations = []
    if mistakes:
        behavioral_observations.append(
            {
                "title": mistakes[0][0],
                "description": f"Observed {mistakes[0][1]} time(s) in recent reviewed trades.",
                "confidence": _confidence(mistakes[0][1]),
                "evidence_count": mistakes[0][1],
            }
        )
    if risk_report.get("highest_confidence_warnings"):
        w = risk_report["highest_confidence_warnings"][0]
        behavioral_observations.append(
            {
                "title": w.get("warning"),
                "description": f"Risk engine warning from {w.get('source')}.",
                "confidence": w.get("confidence") or 0.2,
                "evidence_count": w.get("intervention_level") or 1,
            }
        )

    return {
        "ok": True,
        "sample_size": sample,
        "execution_score": avg_execution,
        "discipline_score": avg_discipline,
        "process_score": avg_process,
        "confidence": _confidence(sample),
        "recent_reviews": reviews[-10:],
        "recurring_mistakes": [
            {"title": k, "description": f"Observed {v} time(s).", "confidence": _confidence(v), "evidence_count": v}
            for k, v in mistakes[:5]
        ],
        "recurring_strengths": [
            {"title": k, "description": f"Observed {v} time(s).", "confidence": _confidence(v), "evidence_count": v}
            for k, v in strengths[:5]
        ],
        "behavioral_observations": behavioral_observations,
        "playbook_context": {
            "win_rate": playbook_stats.get("win_rate"),
            "expectancy": playbook_stats.get("expectancy"),
        },
        "memory_context_count": len(memory_items),
        "discovery_context_count": len(discoveries),
        "generated_at_utc": datetime.utcnow().isoformat(),
    }


def build_journal_memory_updates(journal_report: dict[str, Any]) -> list[dict[str, Any]]:
    updates = []
    sample = int(journal_report.get("sample_size") or 0)
    if sample:
        updates.append(
            {
                "memory_type": "journal_memory",
                "belief_type": "fact",
                "topic": "reviewed_closed_trades",
                "statement": f"Journal AI reviewed {sample} closed trade(s).",
                "confidence": _confidence(sample),
                "evidence_count": sample,
                "source_module": "journal_engine",
            }
        )
    for m in journal_report.get("recurring_mistakes") or []:
        updates.append(
            {
                "memory_type": "behavioral_memory",
                "belief_type": "hypothesis" if m["confidence"] < 0.55 else "inference",
                "topic": "journal_mistake",
                "statement": m["title"],
                "confidence": m["confidence"],
                "evidence_count": m["evidence_count"],
                "source_module": "journal_engine",
            }
        )
    for s in journal_report.get("recurring_strengths") or []:
        updates.append(
            {
                "memory_type": "behavioral_memory",
                "belief_type": "inference",
                "topic": "journal_strength",
                "statement": s["title"],
                "confidence": s["confidence"],
                "evidence_count": s["evidence_count"],
                "source_module": "journal_engine",
            }
        )
    return updates
