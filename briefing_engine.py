from __future__ import annotations

from datetime import datetime
from typing import Any
import uuid


BRIEFING_TYPES = ("morning", "midday", "end_of_day", "weekly")
BRIEFING_PRIORITIES = ("Critical", "High", "Medium", "Low")


def _priority_from_sources(alerts: list[dict[str, Any]], risk: dict[str, Any], discoveries: list[dict[str, Any]]) -> str:
    if any((a.get("priority") == "Critical" or a.get("urgency") == "immediate") and a.get("status") == "OPEN" for a in alerts):
        return "Critical"
    if (risk or {}).get("overall_risk_label") == "High Risk":
        return "High"
    if any(d.get("priority") in ("Critical", "High") for d in discoveries):
        return "High"
    if alerts or discoveries:
        return "Medium"
    return "Low"


def _top(items: list[dict[str, Any]], n: int = 5) -> list[dict[str, Any]]:
    return items[:n] if items else []


def _section(title: str, bullets: list[str], priority: str = "Medium") -> dict[str, Any]:
    return {"title": title, "priority": priority, "bullets": bullets or ["No major update."]}


def _alert_bullets(alerts: list[dict[str, Any]]) -> list[str]:
    out = []
    for a in _top(alerts, 5):
        out.append(f"{a.get('priority') or a.get('urgency') or 'Alert'}: {a.get('ticker') or ''} - {a.get('message')}")
    return out


def _discovery_bullets(discoveries: list[dict[str, Any]]) -> list[str]:
    out = []
    for d in _top(discoveries, 5):
        out.append(f"{d.get('priority') or d.get('importance') or 'Discovery'} {d.get('category') or ''}: {d.get('title')} - {d.get('description')}")
    return out


def _risk_bullets(risk: dict[str, Any]) -> list[str]:
    out = []
    if not risk:
        return ["Risk profile unavailable."]
    out.append(f"Overall risk: {risk.get('overall_risk_label')} ({(risk.get('overall_risk_score') or 0):.0f}).")
    for w in _top(risk.get("highest_confidence_warnings") or [], 4):
        out.append(f"Level {w.get('intervention_level')}: {w.get('warning')}")
    return out


def _journal_bullets(journal: dict[str, Any]) -> list[str]:
    if not journal:
        return ["Journal AI unavailable."]
    out = [
        f"Execution score: {journal.get('execution_score') if journal.get('execution_score') is not None else 'n/a'}",
        f"Discipline score: {journal.get('discipline_score') if journal.get('discipline_score') is not None else 'n/a'}",
        f"Process score: {journal.get('process_score') if journal.get('process_score') is not None else 'n/a'}",
    ]
    for m in _top(journal.get("recurring_mistakes") or [], 3):
        out.append(f"Mistake: {m.get('title')} ({m.get('evidence_count')} evidence)")
    for s in _top(journal.get("recurring_strengths") or [], 2):
        out.append(f"Strength: {s.get('title')} ({s.get('evidence_count')} evidence)")
    return out


def _playbook_bullets(learning: dict[str, Any]) -> list[str]:
    stats = (learning or {}).get("stats") or {}
    out = [
        f"Completed plan sample size: {stats.get('sample_size') or 0}",
        f"Win rate: {stats.get('win_rate') * 100:.0f}%" if stats.get("win_rate") is not None else "Win rate: n/a",
        f"Expectancy proxy: {stats.get('expectancy'):.2f}" if stats.get("expectancy") is not None else "Expectancy proxy: n/a",
    ]
    for s in _top(stats.get("strengths") or [], 3):
        out.append(f"Strength: {s.get('title')} - {s.get('description')}")
    for w in _top(stats.get("weaknesses") or [], 3):
        out.append(f"Weakness: {w.get('title')} - {w.get('description')}")
    return out


def _plan_bullets(plans: list[dict[str, Any]]) -> list[str]:
    active = [p for p in plans if p.get("status") in ("ACTIVE", "TRIGGERED")]
    if not active:
        return ["No active trade plans."]
    return [
        f"{p.get('ticker')} {p.get('plan_style')} plan: status {p.get('status')}, trigger {p.get('trigger_price')}, stop {p.get('stop_price')}"
        for p in _top(active, 6)
    ]


def _research_bullets(reports: list[dict[str, Any]]) -> list[str]:
    if not reports:
        return ["No saved research report yet. Generate a Research report to feed valuation, peer benchmarking, and conviction into briefings."]
    out = []
    for row in _top(reports, 5):
        report = row.get("report_json") or {}
        conviction = report.get("conviction") or {}
        valuation = report.get("valuation") or {}
        peer = report.get("peer_benchmarking") or {}
        out.append(
            f"{report.get('ticker') or row.get('ticker')}: {report.get('verdict') or row.get('verdict')} | "
            f"Conviction {conviction.get('score', 'n/a')} ({conviction.get('rating', 'n/a')}) | "
            f"Valuation {valuation.get('rating', 'n/a')} | Peers {peer.get('rating', 'n/a')}"
        )
    return out


def build_briefing(briefing_type: str, context: dict[str, Any]) -> dict[str, Any]:
    briefing_type = (briefing_type or "morning").lower()
    if briefing_type not in BRIEFING_TYPES:
        briefing_type = "morning"

    alerts = context.get("alerts") or []
    open_alerts = [a for a in alerts if a.get("status") == "OPEN"]
    discoveries = context.get("discoveries") or []
    risk = context.get("risk") or {}
    journal = context.get("journal") or {}
    learning = context.get("learning") or {}
    plans = context.get("trade_plans") or []
    research_reports = context.get("research_reports") or []

    priority = _priority_from_sources(open_alerts, risk, discoveries)
    generated_at = datetime.utcnow().isoformat()

    if briefing_type == "morning":
        title = "Morning Briefing"
        lead = "Most important now: review open alerts, active trade plans, and any high-priority risk/discovery items before taking new risk."
        sections = [
            _section("Market overview", ["Market regime integration is pending; use scanner/risk context until market regime engine is live."], "Medium"),
            _section("Active trade plans", _plan_bullets(plans), "High" if plans else "Low"),
            _section("Highest priority alerts", _alert_bullets(open_alerts), "Critical" if priority == "Critical" else "High"),
            _section("Top discoveries", _discovery_bullets(discoveries), "High"),
            _section("Top risks", _risk_bullets(risk), "High"),
            _section("Research conviction", _research_bullets(research_reports), "High" if research_reports else "Low"),
            _section("Personalized coaching", _playbook_bullets(learning), "Medium"),
        ]
    elif briefing_type == "midday":
        title = "Midday Briefing"
        lead = "Most important now: check triggered alerts, changing risk, and whether active plans are still valid."
        sections = [
            _section("Triggered / open alerts", _alert_bullets(open_alerts), "High"),
            _section("Changing conditions", _risk_bullets(risk), "High"),
            _section("Active opportunities", _plan_bullets(plans), "Medium"),
            _section("Research watchlist", _research_bullets(research_reports), "Medium"),
            _section("Discovery updates", _discovery_bullets(discoveries), "Medium"),
        ]
    elif briefing_type == "end_of_day":
        title = "End-of-Day Review"
        lead = "Most important now: review process quality, trade outcomes, risk behavior, and lessons before tomorrow."
        sections = [
            _section("Trade review summary", _journal_bullets(journal), "High"),
            _section("Risk summary", _risk_bullets(risk), "High"),
            _section("Lessons learned", _playbook_bullets(learning), "Medium"),
            _section("Discoveries generated", _discovery_bullets(discoveries), "Medium"),
            _section("Research generated", _research_bullets(research_reports), "Medium"),
            _section("Tomorrow preparation", ["Carry forward only the plans that remain valid. Remove stale plans and review high-risk warnings first."], "Medium"),
        ]
    else:
        title = "Weekly Briefing"
        lead = "Most important now: update the playbook, review strengths/weaknesses, and identify the highest-impact discoveries."
        sections = [
            _section("Playbook changes", _playbook_bullets(learning), "High"),
            _section("Memory graph changes", [f"{len(context.get('memory') or [])} memory item(s) currently available."], "Medium"),
            _section("Strengths and weaknesses", _playbook_bullets(learning), "High"),
            _section("Highest impact discoveries", _discovery_bullets(discoveries), "High"),
            _section("Research conviction changes", _research_bullets(research_reports), "High"),
            _section("Risk posture", _risk_bullets(risk), "High"),
        ]

    coaching = {
        "strengths": ((learning.get("stats") or {}).get("strengths") or [])[:3],
        "weaknesses": ((learning.get("stats") or {}).get("weaknesses") or [])[:3],
        "opportunities": [d for d in discoveries if d.get("category") in ("Opportunity", "Edge")][:3],
        "risks": risk.get("highest_confidence_warnings") or [],
    }

    return {
        "id": str(uuid.uuid4()),
        "briefing_type": briefing_type,
        "title": title,
        "priority": priority,
        "lead": lead,
        "sections": sections,
        "coaching": coaching,
        "source_counts": {
            "alerts": len(alerts),
            "open_alerts": len(open_alerts),
            "discoveries": len(discoveries),
            "trade_plans": len(plans),
            "memory_items": len(context.get("memory") or []),
            "research_reports": len(research_reports),
        },
        "generated_at_utc": generated_at,
    }
