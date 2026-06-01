from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
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


DEFAULT_RULES = [
    {"name": "No chasing", "rule_type": "max_entry_extension_pct", "threshold": 0.05, "active": True},
    {"name": "Minimum 2:1 reward/risk", "rule_type": "min_reward_risk", "threshold": 2.0, "active": True},
    {"name": "Maximum 1% account risk", "rule_type": "max_account_risk_pct", "threshold": 0.01, "active": True},
    {"name": "No revenge trading", "rule_type": "tag_absent", "tag": "revenge", "active": True},
]


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


def _tags(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(x).strip().lower() for x in value if str(x).strip()]
    if isinstance(value, str):
        try:
            import json
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return _tags(parsed)
        except Exception:
            pass
        return [x.strip().lower() for x in value.replace(";", ",").split(",") if x.strip()]
    return []


def _rr_from_trade_plan(trade: dict[str, Any], plan: dict[str, Any] | None) -> float | None:
    if plan and plan.get("risk_reward") is not None:
        return _num(plan.get("risk_reward"))
    entry = _num(trade.get("entry_price"))
    stop = _num(trade.get("stop"))
    target = _num(trade.get("target_1") or trade.get("target") or trade.get("win_px"))
    if entry and stop and target:
        risk = max(entry - stop, 0.0001)
        return max(0, target - entry) / risk
    return None


def _score_rule(trade: dict[str, Any], rule: dict[str, Any], plan: dict[str, Any] | None, mission: dict[str, Any] | None = None) -> dict[str, Any]:
    rule_type = rule.get("rule_type") or "custom"
    passed = True
    evidence = "No direct evidence available."
    severity = "Low"
    entry = _num(trade.get("entry_price"))
    trigger = _num(trade.get("trigger") or (plan or {}).get("trigger_price"))
    if rule_type == "max_entry_extension_pct":
        threshold = _num(rule.get("threshold"), 0.05)
        if entry and trigger:
            extension = (entry - trigger) / trigger
            passed = extension <= threshold
            evidence = f"Entry extension {extension * 100:.1f}% vs max {threshold * 100:.1f}%."
            severity = "High" if not passed and extension >= threshold * 2 else "Medium" if not passed else "Low"
    elif rule_type == "min_reward_risk":
        threshold = _num(rule.get("threshold"), 2.0)
        rr = _rr_from_trade_plan(trade, plan)
        if rr is not None:
            passed = rr >= threshold
            evidence = f"Reward/risk {rr:.2f} vs minimum {threshold:.2f}."
            severity = "High" if not passed and rr < 1.2 else "Medium" if not passed else "Low"
    elif rule_type == "max_account_risk_pct":
        threshold = _num(rule.get("threshold"), _num((mission or {}).get("max_risk_per_trade"), 0.01))
        account = _num((mission or {}).get("account_size"))
        entry_px = _num(trade.get("entry_price"))
        stop = _num(trade.get("stop") or (plan or {}).get("stop_price"))
        shares = _num(trade.get("shares"))
        if account and entry_px and stop and shares:
            risk_pct = abs(entry_px - stop) * shares / account
            passed = risk_pct <= threshold
            evidence = f"Account risk {risk_pct * 100:.2f}% vs max {threshold * 100:.2f}%."
            severity = "High" if not passed else "Low"
    elif rule_type == "tag_absent":
        tag = str(rule.get("tag") or "").lower()
        combined_tags = _tags(trade.get("mistake_tags")) + _tags(trade.get("emotion_tags")) + _tags(trade.get("review_flags")) + _tags(trade.get("review_text"))
        if tag:
            passed = tag not in " ".join(combined_tags)
            evidence = f"Tag '{tag}' {'not found' if passed else 'found'} in trade notes/tags."
            severity = "High" if not passed else "Low"
    elif rule_type == "preferred_setup":
        preferred = [str(x).lower() for x in (mission or {}).get("preferred_setups", [])]
        setup = str(trade.get("subtype") or "").lower()
        if preferred:
            passed = any(p in setup for p in preferred)
            evidence = f"Setup '{setup or 'unknown'}' compared with preferred setups: {', '.join(preferred)}."
    else:
        evidence = rule.get("description") or rule.get("name") or "Custom rule requires manual review."
    return {"rule": rule.get("name") or rule_type, "passed": bool(passed), "evidence": evidence, "severity": severity, "rule_type": rule_type}


def score_trade_rules(trade: dict[str, Any], rules: list[dict[str, Any]], plan: dict[str, Any] | None, mission: dict[str, Any] | None = None) -> dict[str, Any]:
    active = [r for r in (rules or DEFAULT_RULES) if r.get("active", True)]
    results = [_score_rule(trade, r, plan, mission=mission) for r in active]
    violations = [r for r in results if not r["passed"]]
    successes = [r for r in results if r["passed"]]
    score = (len(successes) / len(results) * 100) if results else 50
    return {"score": round(score), "results": results, "violations": violations, "successes": successes}


def review_trade(trade: dict[str, Any], plans: list[dict[str, Any]], mission: dict[str, Any] | None = None, rules: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    pnl = _num(trade.get("pnl_dollars"))
    pnl_pct = _num(trade.get("pnl_pct"))
    entry = _num(trade.get("entry_price"))
    exit_px = _num(trade.get("exit_price"))
    plan = _match_plan_for_trade(trade, plans)
    rule_result = score_trade_rules(trade, rules or DEFAULT_RULES, plan, mission=mission)
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

    mistake_tags = _tags(trade.get("mistake_tags") or trade.get("review_text"))
    emotion_tags = _tags(trade.get("emotion_tags") or trade.get("review_text"))
    if any("chasing" in x or "fomo" in x for x in mistake_tags + emotion_tags):
        mistakes.append("Chasing/FOMO behavior tagged or implied.")
    if any("revenge" in x for x in mistake_tags + emotion_tags):
        mistakes.append("Revenge trading behavior tagged or implied.")
    if any("emotional" in x or "tilt" in x for x in emotion_tags):
        mistakes.append("Emotional trading tagged or implied.")
    for violation in rule_result["violations"]:
        mistakes.append(f"Rule violation: {violation['rule']} - {violation['evidence']}")
    for success in rule_result["successes"][:3]:
        strengths.append(f"Rule followed: {success['rule']}.")

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
    discipline_score += (rule_result["score"] - 70) * 0.25
    discipline_score = max(0, min(100, discipline_score))

    process_score = 65
    if trade.get("review_text"):
        process_score += 10
    if trade.get("plan"):
        process_score += 8
    if not plan:
        process_score -= 8
    if mission:
        process_score += 5
    process_score += (rule_result["score"] - 70) * 0.15
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
        "behavioral_score": max(0, min(100, 80 - len([m for m in mistakes if any(k in m.lower() for k in ("chasing", "revenge", "emotional", "fomo"))]) * 15)),
        "goal_adherence_score": rule_result["score"],
        "rule_results": rule_result["results"],
        "rule_violations": rule_result["violations"],
        "rule_successes": rule_result["successes"],
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
    mission_profile: dict[str, Any] | None = None,
    trading_rules: list[dict[str, Any]] | None = None,
    chat_memory: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    closed = _closed_trades(trades)
    reviews = [review_trade(t, plans, mission=mission_profile, rules=trading_rules) for t in closed[-25:]]
    sample = len(reviews)

    avg_execution = sum(r["execution_score"] for r in reviews) / sample if sample else None
    avg_discipline = sum(r["discipline_score"] for r in reviews) / sample if sample else None
    avg_process = sum(r["process_score"] for r in reviews) / sample if sample else None
    avg_behavioral = sum(r.get("behavioral_score", 50) for r in reviews) / sample if sample else None
    avg_goal = sum(r.get("goal_adherence_score", 50) for r in reviews) / sample if sample else None

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

    weekly = build_weekly_review(closed, reviews)
    setup_breakdown = setup_performance_breakdown(closed)
    action_plan = coaching_action_plan(reviews, weekly, setup_breakdown, mission_profile)

    return {
        "ok": True,
        "sample_size": sample,
        "execution_score": avg_execution,
        "discipline_score": avg_discipline,
        "process_score": avg_process,
        "behavioral_score": avg_behavioral,
        "goal_adherence_score": avg_goal,
        "confidence": _confidence(sample),
        "recent_reviews": reviews[-10:],
        "weekly_review": weekly,
        "setup_performance": setup_breakdown,
        "coaching_action_plan": action_plan,
        "mission_profile": mission_profile or {},
        "trading_rules": trading_rules or DEFAULT_RULES,
        "journal_chat_memory_count": len(chat_memory or []),
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


def setup_performance_breakdown(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in _closed_trades(trades):
        groups[(t.get("subtype") or "unknown").lower()].append(t)
    rows = []
    for setup, items in groups.items():
        pnls = [_num(t.get("pnl_dollars")) for t in items if t.get("pnl_dollars") is not None]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        rows.append({
            "setup_type": setup,
            "sample_size": len(items),
            "win_rate": len(wins) / len(pnls) if pnls else None,
            "average_gain": sum(wins) / len(wins) if wins else None,
            "average_loss": sum(losses) / len(losses) if losses else None,
            "expectancy": sum(pnls) / len(pnls) if pnls else None,
            "total_pnl": sum(pnls) if pnls else 0,
        })
    rows.sort(key=lambda r: r["total_pnl"], reverse=True)
    return rows


def build_weekly_review(trades: list[dict[str, Any]], reviews: list[dict[str, Any]]) -> dict[str, Any]:
    cutoff = datetime.utcnow() - timedelta(days=7)
    recent = []
    for t in _closed_trades(trades):
        try:
            dt = datetime.fromisoformat(str(t.get("created_at_utc") or "").replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            dt = datetime.utcnow()
        if dt >= cutoff:
            recent.append(t)
    recent = recent or _closed_trades(trades)[-10:]
    best = max(recent, key=lambda t: _num(t.get("pnl_dollars")), default=None)
    worst = min(recent, key=lambda t: _num(t.get("pnl_dollars")), default=None)
    violations = []
    for r in reviews:
        violations.extend(r.get("rule_violations") or [])
    breakdown = setup_performance_breakdown(recent)
    return {
        "best_trade": best,
        "worst_trade": worst,
        "biggest_mistake": (violations[0]["rule"] if violations else "No major rule violation detected."),
        "best_setup": breakdown[0] if breakdown else None,
        "worst_setup": breakdown[-1] if breakdown else None,
        "rule_violations": violations[:10],
        "goal_progress": "Mission profile active." if reviews else "Add trades to measure progress.",
        "next_week_focus": "Focus on the highest-frequency rule violation." if violations else "Keep collecting clean, rule-tagged trades.",
    }


def coaching_action_plan(reviews: list[dict[str, Any]], weekly: dict[str, Any], setup_breakdown: list[dict[str, Any]], mission_profile: dict[str, Any] | None) -> dict[str, str]:
    violations = weekly.get("rule_violations") or []
    strengths = []
    for r in reviews:
        strengths.extend(r.get("strengths") or [])
    best_setup = setup_breakdown[0]["setup_type"] if setup_breakdown else "your best-tested setup"
    focus_rule = violations[0]["rule"] if violations else (mission_profile or {}).get("behavioral_goals") or "follow your written rules"
    return {
        "stop_doing": violations[0]["evidence"] if violations else "Stop taking untagged trades without a written reason.",
        "keep_doing": strengths[0] if strengths else f"Keep tracking outcomes by setup type, especially {best_setup}.",
        "improve_tomorrow": "Write the setup, risk/reward, and emotional state before entry.",
        "weekly_rule_focus": str(focus_rule),
    }


def answer_journal_question(question: str, journal_report: dict[str, Any], playbook_stats: dict[str, Any], mission_profile: dict[str, Any], chat_memory: list[dict[str, Any]] | None = None) -> str:
    q = (question or "").lower()
    if "losing" in q or "lose" in q:
        return f"Your current weak spot is {journal_report.get('weekly_review', {}).get('biggest_mistake')}. Focus next on: {journal_report.get('coaching_action_plan', {}).get('improve_tomorrow')}."
    if "weakness" in q or "mistakes" in q:
        mistakes = journal_report.get("recurring_mistakes") or []
        return mistakes[0]["title"] if mistakes else "No repeated mistake has enough evidence yet. Keep tagging trades."
    if "strength" in q:
        strengths = journal_report.get("recurring_strengths") or []
        return strengths[0]["title"] if strengths else "No repeated strength has enough evidence yet."
    if "setup" in q and ("money" in q or "most" in q):
        setups = journal_report.get("setup_performance") or []
        return f"Best setup so far: {setups[0]['setup_type']} with total P/L {setups[0]['total_pnl']:.2f}." if setups else "No closed setup data yet."
    if "improve" in q or "next week" in q:
        plan = journal_report.get("coaching_action_plan") or {}
        return f"Stop: {plan.get('stop_doing')} Keep: {plan.get('keep_doing')} Improve tomorrow: {plan.get('improve_tomorrow')} Weekly rule: {plan.get('weekly_rule_focus')}."
    return f"Praetor's journal read: execution {journal_report.get('execution_score')}, discipline {journal_report.get('discipline_score')}, process {journal_report.get('process_score')}. Ask about losses, strengths, mistakes, or setups for a sharper answer."


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
