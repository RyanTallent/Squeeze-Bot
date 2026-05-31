from __future__ import annotations

from datetime import datetime
from typing import Any


def _first(items: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    return items[0] if items else None


def _priority_rank(item: dict[str, Any]) -> tuple[int, float]:
    priority_order = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
    priority = item.get("priority") or item.get("urgency") or "Low"
    if isinstance(priority, str):
        priority_key = priority.title()
    else:
        priority_key = "Low"
    confidence = item.get("confidence")
    try:
        confidence_val = float(confidence) if confidence is not None else 0.0
    except Exception:
        confidence_val = 0.0
    return (priority_order.get(priority_key, 3), -confidence_val)


def _latest_by_created(items: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    if not items:
        return None
    return sorted(items, key=lambda x: str(x.get("created_at_utc") or ""), reverse=True)[0]


def _top_risk_warning(risk: dict[str, Any] | None) -> dict[str, Any] | None:
    warnings = (risk or {}).get("highest_confidence_warnings") or []
    if not warnings:
        return None
    return sorted(warnings, key=lambda w: (-(w.get("confidence") or 0), -(w.get("intervention_level") or 0)))[0]


def _recent_journal_lesson(journal: dict[str, Any] | None) -> dict[str, Any] | None:
    reviews = (journal or {}).get("recent_reviews") or []
    for review in reversed(reviews):
        lessons = review.get("lessons") or []
        if lessons:
            return {"ticker": review.get("ticker"), "lesson": lessons[0], "trade_id": review.get("trade_id")}
    return None


def _latest_research(reports: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    if not reports:
        return None
    return sorted(reports, key=lambda x: str(x.get("created_at_utc") or ""), reverse=True)[0]


def _feed_item(kind: str, title: str, description: str, priority: str = "Medium") -> dict[str, Any]:
    return {
        "kind": kind,
        "title": title,
        "description": description,
        "priority": priority,
        "created_at_utc": datetime.utcnow().isoformat(),
    }


def build_command_center(data: dict[str, Any]) -> dict[str, Any]:
    alerts = data.get("alerts") or []
    active_alerts = [a for a in alerts if a.get("status") == "OPEN"]
    discoveries = data.get("discoveries") or []
    briefings = data.get("briefings") or []
    committee_runs = data.get("committee_runs") or []
    trade_plans = data.get("trade_plans") or []
    active_plans = [p for p in trade_plans if p.get("status") in ("ACTIVE", "TRIGGERED")]
    learning = data.get("learning") or {}
    stats = learning.get("stats") or {}
    risk = data.get("risk") or {}
    journal = data.get("journal") or {}
    memory = data.get("memory") or []
    portfolio = data.get("portfolio") or {}
    research_reports = data.get("research_reports") or []

    highest_alert = _first(sorted(active_alerts, key=_priority_rank))
    top_discovery = _first(sorted(discoveries, key=lambda d: (d.get("priority") != "Critical", d.get("priority") != "High", -(d.get("impact_score") or 0))))
    latest_briefing = _latest_by_created(briefings)
    latest_committee = _latest_by_created(committee_runs)
    top_risk = _top_risk_warning(risk)
    recent_lesson = _recent_journal_lesson(journal)
    latest_research = _latest_research(research_reports)

    strengths = stats.get("strengths") or []
    weaknesses = stats.get("weaknesses") or []
    hypotheses = [m for m in memory if m.get("belief_type") == "hypothesis"]

    feed: list[dict[str, Any]] = []
    if top_risk:
        feed.append(_feed_item("risk", "Risk AI warning", top_risk.get("warning") or "Risk warning detected.", "High"))
    if top_discovery:
        feed.append(_feed_item("discovery", top_discovery.get("title") or "Discovery", top_discovery.get("description") or "", top_discovery.get("priority") or "Medium"))
    if latest_committee:
        feed.append(_feed_item("committee", "Committee consensus", f"Consensus: {latest_committee.get('consensus')}. {latest_committee.get('final_recommendation')}", "Medium"))
    if highest_alert:
        feed.append(_feed_item("alert", "Highest priority alert", highest_alert.get("message") or "", highest_alert.get("priority") or "High"))
    if recent_lesson:
        feed.append(_feed_item("journal", f"Journal lesson: {recent_lesson.get('ticker')}", recent_lesson.get("lesson") or "", "Medium"))
    if strengths:
        feed.append(_feed_item("learning", "Playbook strength detected", strengths[0].get("description") or strengths[0].get("title") or "", "Medium"))
    if weaknesses:
        feed.append(_feed_item("learning", "Playbook weakness detected", weaknesses[0].get("description") or weaknesses[0].get("title") or "", "High"))
    if portfolio.get("risk", {}).get("warnings"):
        feed.append(_feed_item("portfolio", "Portfolio risk note", portfolio["risk"]["warnings"][0], "Medium"))
    if latest_research:
        report = latest_research.get("report_json") or {}
        conviction = report.get("conviction") or {}
        feed.append(
            _feed_item(
                "research",
                f"Research conviction: {report.get('ticker') or latest_research.get('ticker')}",
                f"{report.get('verdict') or latest_research.get('verdict')} | {conviction.get('rating', 'Conviction pending')} ({conviction.get('score', 'n/a')}/100).",
                "High" if (conviction.get("score") or 0) >= 68 else "Medium",
            )
        )

    if highest_alert:
        rec = f"Address the highest priority alert first: {highest_alert.get('message')}"
        rec_priority = highest_alert.get("priority") or "High"
    elif top_risk:
        rec = f"Focus on risk control: {top_risk.get('warning')}"
        rec_priority = "High"
    elif latest_committee and latest_committee.get("consensus") in ("Bearish", "Strong Bearish"):
        rec = f"Committee is cautious: {latest_committee.get('final_recommendation')}"
        rec_priority = "High"
    elif latest_research and ((latest_research.get("report_json") or {}).get("conviction") or {}).get("score", 50) < 45:
        report = latest_research.get("report_json") or {}
        rec = f"Research conviction is weak for {report.get('ticker') or latest_research.get('ticker')}; review valuation, peer rank, and reasons against before adding exposure."
        rec_priority = "High"
    elif top_discovery:
        rec = f"Review top discovery: {top_discovery.get('title')}"
        rec_priority = top_discovery.get("priority") or "Medium"
    else:
        rec = "No critical issue detected. Review active plans and avoid forcing trades without confirmation."
        rec_priority = "Low"

    return {
        "ok": True,
        "generated_at_utc": datetime.utcnow().isoformat(),
        "highest_priority_alert": highest_alert,
        "top_discovery": top_discovery,
        "latest_briefing": latest_briefing,
        "latest_committee_consensus": latest_committee,
        "top_risk_warning": top_risk,
        "active_trade_plans": active_plans[:8],
        "playbook_snapshot": {
            "sample_size": stats.get("sample_size") or 0,
            "win_rate": stats.get("win_rate"),
            "expectancy": stats.get("expectancy"),
            "strengths": strengths[:3],
            "weaknesses": weaknesses[:3],
        },
        "recent_journal_lesson": recent_lesson,
        "monitoring_summary": {
            "monitored_plans": len(active_plans),
            "active_alerts": len(active_alerts),
            "triggered_plans": len([p for p in active_plans if p.get("status") == "TRIGGERED"]),
        },
        "portfolio_overview": {
            "portfolio_value": portfolio.get("portfolio_value"),
            "top_holding": portfolio.get("top_holding"),
            "sector_exposure": (portfolio.get("sector_exposure") or [])[:3],
            "risk": portfolio.get("risk"),
        },
        "research_overview": {
            "latest": latest_research,
            "latest_ticker": (latest_research.get("report_json") or {}).get("ticker") if latest_research else None,
            "verdict": (latest_research.get("report_json") or {}).get("verdict") if latest_research else None,
            "conviction": (latest_research.get("report_json") or {}).get("conviction") if latest_research else None,
            "valuation": (latest_research.get("report_json") or {}).get("valuation") if latest_research else None,
            "peer_benchmarking": (latest_research.get("report_json") or {}).get("peer_benchmarking") if latest_research else None,
            "report_count": len(research_reports),
        },
        "learning_progress": {
            "strengths": strengths[:3],
            "weaknesses": weaknesses[:3],
            "hypotheses": hypotheses[:5],
            "memory_count": len(memory),
        },
        "risk_overview": {
            "overall_risk_label": risk.get("overall_risk_label"),
            "overall_risk_score": risk.get("overall_risk_score"),
            "top_warning": top_risk,
        },
        "committee_overview": {
            "consensus": latest_committee.get("consensus") if latest_committee else None,
            "confidence": latest_committee.get("confidence") if latest_committee else None,
            "strongest_bullish_argument": (latest_committee.get("synthesis_json") or {}).get("strongest_bullish_argument") if latest_committee else None,
            "strongest_bearish_argument": (latest_committee.get("synthesis_json") or {}).get("strongest_bearish_argument") if latest_committee else None,
            "final_recommendation": latest_committee.get("final_recommendation") if latest_committee else None,
        },
        "briefing_overview": {
            "latest": latest_briefing,
            "morning": _first([b for b in briefings if b.get("briefing_type") == "morning"]),
            "midday": _first([b for b in briefings if b.get("briefing_type") == "midday"]),
            "end_of_day": _first([b for b in briefings if b.get("briefing_type") == "end_of_day"]),
            "weekly": _first([b for b in briefings if b.get("briefing_type") == "weekly"]),
        },
        "recommendation_of_day": {"priority": rec_priority, "text": rec},
        "intelligence_feed": feed[:12],
        "future_modules": ["SMS status", "Wealth AI", "Portfolio Intelligence", "Research AI", "Options Intelligence"],
    }
