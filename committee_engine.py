from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any
import uuid


STANCES = ("Strong Bullish", "Bullish", "Neutral", "Bearish", "Strong Bearish")
STANCE_SCORE = {
    "Strong Bearish": -2,
    "Bearish": -1,
    "Neutral": 0,
    "Bullish": 1,
    "Strong Bullish": 2,
}


@dataclass
class CommitteeVote:
    member: str
    role: str
    stance: str
    confidence: float
    supporting_evidence: list[str]
    concerns: list[str]
    recommendation: str


def _confidence(evidence_count: int, base: float = 0.25) -> float:
    return max(0.0, min(0.95, base + evidence_count * 0.06))


def _stance_from_score(score: float) -> str:
    if score >= 1.4:
        return "Strong Bullish"
    if score >= 0.45:
        return "Bullish"
    if score <= -1.4:
        return "Strong Bearish"
    if score <= -0.45:
        return "Bearish"
    return "Neutral"


def _top(items: list[dict[str, Any]], n: int = 4) -> list[dict[str, Any]]:
    return items[:n] if items else []


def _stats(context: dict[str, Any]) -> dict[str, Any]:
    return ((context.get("learning") or {}).get("stats") or {})


def _risk(context: dict[str, Any]) -> dict[str, Any]:
    return context.get("risk") or {}


def _journal(context: dict[str, Any]) -> dict[str, Any]:
    return context.get("journal") or {}


def _discoveries(context: dict[str, Any]) -> list[dict[str, Any]]:
    return context.get("discoveries") or []


def _memory(context: dict[str, Any]) -> list[dict[str, Any]]:
    return context.get("memory") or []


def _plans(context: dict[str, Any]) -> list[dict[str, Any]]:
    return context.get("trade_plans") or []


def _briefings(context: dict[str, Any]) -> list[dict[str, Any]]:
    return context.get("briefings") or []


def _research_reports(context: dict[str, Any]) -> list[dict[str, Any]]:
    return context.get("research_reports") or []


def _latest_research_report(context: dict[str, Any]) -> dict[str, Any] | None:
    reports = _research_reports(context)
    if not reports:
        return None
    return sorted(reports, key=lambda r: str(r.get("created_at_utc") or ""), reverse=True)[0]


def research_analyst_vote(context: dict[str, Any]) -> CommitteeVote:
    latest = _latest_research_report(context)
    discoveries = _discoveries(context)
    edge = [d for d in discoveries if d.get("category") in ("Edge", "Opportunity")]
    risk = [d for d in discoveries if d.get("category") in ("Risk", "Behavioral")]
    evidence = [f"{d.get('title')}: {d.get('description')}" for d in _top(edge, 3)]
    concerns = [f"{d.get('title')}: {d.get('description')}" for d in _top(risk, 3)]
    score = len(edge) * 0.55 - len(risk) * 0.45
    if latest:
        report = latest.get("report_json") or {}
        conviction = report.get("conviction") or {}
        valuation = report.get("valuation") or {}
        peer = report.get("peer_benchmarking") or {}
        conviction_score = conviction.get("score")
        if conviction_score is not None:
            score += ((conviction_score - 50) / 50) * 1.15
        evidence.append(
            f"{report.get('ticker')}: {report.get('verdict')} / {conviction.get('rating')} ({conviction.get('score')} conviction)."
        )
        if valuation.get("rating"):
            evidence.append(f"Valuation: {valuation.get('rating')} ({valuation.get('score')}/100).")
        if peer.get("rating"):
            evidence.append(f"Peer benchmarking: {peer.get('rating')} ({peer.get('score')}/100).")
        concerns.extend((conviction.get("reasons_against") or [])[:3])
    if not discoveries and not latest:
        concerns.append("Research/discovery evidence is limited.")
    return CommitteeVote(
        member="Research Analyst",
        role="Equity research and thesis quality",
        stance=_stance_from_score(score),
        confidence=_confidence(len(evidence) + len(concerns)),
        supporting_evidence=evidence or ["No strong research edge discovered yet."],
        concerns=concerns or ["No major research concern surfaced."],
        recommendation="Treat the idea as evidence-weighted; require stronger thesis support before increasing conviction.",
    )


def momentum_trader_vote(context: dict[str, Any]) -> CommitteeVote:
    plans = _plans(context)
    active = [p for p in plans if p.get("status") in ("ACTIVE", "TRIGGERED")]
    high_conf = [p for p in active if (p.get("confidence") or 0) >= 75]
    low_conf = [p for p in active if (p.get("confidence") or 0) < 55]
    evidence = [f"{p.get('ticker')} {p.get('plan_style')} plan confidence {p.get('confidence')}" for p in _top(high_conf, 4)]
    concerns = [f"{p.get('ticker')} low-confidence active plan ({p.get('confidence')})" for p in _top(low_conf, 4)]
    score = len(high_conf) * 0.65 - len(low_conf) * 0.55
    if not active:
        concerns.append("No active trade plans to support a momentum stance.")
    return CommitteeVote(
        member="Momentum Trader",
        role="Scanner, execution, and short-term opportunity quality",
        stance=_stance_from_score(score),
        confidence=_confidence(len(active), 0.2),
        supporting_evidence=evidence or ["No high-confidence active momentum plan found."],
        concerns=concerns or ["Momentum evidence is constructive but still needs real-time confirmation."],
        recommendation="Only act on setups with live confirmation, liquidity, and clean invalidation.",
    )


def risk_officer_vote(context: dict[str, Any]) -> CommitteeVote:
    risk = _risk(context)
    warnings = risk.get("highest_confidence_warnings") or []
    label = risk.get("overall_risk_label") or "Medium Risk"
    evidence = [f"Overall risk: {label} ({(risk.get('overall_risk_score') or 0):.0f})"]
    concerns = [w.get("warning") for w in _top(warnings, 5) if w.get("warning")]
    score = 0
    if label == "High Risk":
        score -= 1.5
    elif label == "Medium Risk":
        score -= 0.35
    else:
        score += 0.35
    score -= min(1.0, len(concerns) * 0.25)
    return CommitteeVote(
        member="Risk Officer",
        role="Capital protection and downside control",
        stance=_stance_from_score(score),
        confidence=_confidence(len(warnings), 0.35),
        supporting_evidence=evidence,
        concerns=concerns or ["No high-confidence risk warning currently active."],
        recommendation="Respect risk limits first. If risk is elevated, reduce size or wait for cleaner confirmation.",
    )


def portfolio_manager_vote(context: dict[str, Any]) -> CommitteeVote:
    plans = _plans(context)
    active = [p for p in plans if p.get("status") in ("ACTIVE", "TRIGGERED")]
    tickers = {p.get("ticker") for p in active if p.get("ticker")}
    evidence = [f"{len(active)} active/triggered plan(s), {len(tickers)} unique ticker(s)."]
    concerns = []
    score = 0.15
    if len(active) >= 8:
        score -= 1.0
        concerns.append("Many active plans may stretch attention and concentration.")
    if len(tickers) < len(active):
        score -= 0.35
        concerns.append("Multiple active plans overlap the same ticker(s).")
    return CommitteeVote(
        member="Portfolio Manager",
        role="Allocation, concentration, and portfolio fit",
        stance=_stance_from_score(score),
        confidence=_confidence(len(active), 0.2),
        supporting_evidence=evidence,
        concerns=concerns or ["No major active-plan concentration issue detected."],
        recommendation="Keep total exposure aligned with portfolio-level risk, not just single-setup excitement.",
    )


def macro_strategist_vote(context: dict[str, Any]) -> CommitteeVote:
    briefings = _briefings(context)
    risk = _risk(context)
    evidence = [f"{len(briefings)} briefing run(s) available for macro/context synthesis."]
    concerns = []
    score = 0.0
    if not briefings:
        concerns.append("Market regime engine and briefing history are limited.")
        score -= 0.25
    if (risk.get("overall_risk_label") or "") == "High Risk":
        concerns.append("Risk model is elevated; macro/risk backdrop may not support aggression.")
        score -= 0.45
    return CommitteeVote(
        member="Macro Strategist",
        role="Market regime, breadth, and risk-on/risk-off context",
        stance=_stance_from_score(score),
        confidence=_confidence(len(briefings), 0.15),
        supporting_evidence=evidence,
        concerns=concerns or ["Macro context is neutral until dedicated market regime data is added."],
        recommendation="Avoid over-weighting macro until market regime and breadth modules are live.",
    )


def behavioral_coach_vote(context: dict[str, Any]) -> CommitteeVote:
    journal = _journal(context)
    memory = _memory(context)
    mistakes = journal.get("recurring_mistakes") or []
    strengths = journal.get("recurring_strengths") or []
    weak_memories = [m for m in memory if m.get("belief_type") == "hypothesis" or "weak" in str(m.get("topic"))]
    evidence = [f"{s.get('title')} ({s.get('evidence_count')} evidence)" for s in _top(strengths, 3)]
    concerns = [f"{m.get('title')} ({m.get('evidence_count')} evidence)" for m in _top(mistakes, 3)]
    concerns.extend([m.get("statement") for m in _top(weak_memories, 2) if m.get("statement")])
    score = len(strengths) * 0.35 - len(mistakes) * 0.45 - len(weak_memories) * 0.25
    return CommitteeVote(
        member="Behavioral Coach",
        role="Execution behavior, discipline, and personal pattern fit",
        stance=_stance_from_score(score),
        confidence=_confidence(len(mistakes) + len(strengths) + len(weak_memories), 0.20),
        supporting_evidence=evidence or ["No durable behavioral strength established yet."],
        concerns=concerns or ["No recurring behavioral issue detected yet."],
        recommendation="Compare the decision against journal history before acting; process quality matters more than excitement.",
    )


COMMITTEE_MEMBERS = (
    research_analyst_vote,
    momentum_trader_vote,
    risk_officer_vote,
    portfolio_manager_vote,
    macro_strategist_vote,
    behavioral_coach_vote,
)


def synthesize_committee(votes: list[CommitteeVote]) -> dict[str, Any]:
    scores = [STANCE_SCORE[v.stance] for v in votes]
    avg = sum(scores) / len(scores) if scores else 0
    consensus = _stance_from_score(avg)
    spread = max(scores) - min(scores) if scores else 0
    bullish_votes = [v for v in votes if STANCE_SCORE[v.stance] > 0]
    bearish_votes = [v for v in votes if STANCE_SCORE[v.stance] < 0]
    confidence = sum(v.confidence for v in votes) / len(votes) if votes else 0
    disagreement = (
        "Committee disagreement is elevated; review bull and bear evidence before acting."
        if spread >= 3
        else "Committee views are reasonably aligned."
    )
    strongest_bull = max(bullish_votes, key=lambda v: (STANCE_SCORE[v.stance], v.confidence), default=None)
    strongest_bear = min(bearish_votes, key=lambda v: (STANCE_SCORE[v.stance], -v.confidence), default=None)

    if consensus in ("Strong Bullish", "Bullish"):
        recommendation = "Constructive, but size and timing should still respect risk controls and playbook fit."
    elif consensus in ("Strong Bearish", "Bearish"):
        recommendation = "Do not force the idea. Evidence leans against aggressive action until conditions improve."
    else:
        recommendation = "Neutral. Wait for stronger evidence, cleaner confirmation, or better risk/reward."

    return {
        "consensus": consensus,
        "average_vote_score": avg,
        "confidence": confidence,
        "disagreement_summary": disagreement,
        "strongest_bullish_argument": strongest_bull.supporting_evidence[0] if strongest_bull and strongest_bull.supporting_evidence else "No strong bullish argument.",
        "strongest_bearish_argument": strongest_bear.concerns[0] if strongest_bear and strongest_bear.concerns else "No strong bearish argument.",
        "final_recommendation": recommendation,
    }


def run_investment_committee(context: dict[str, Any]) -> dict[str, Any]:
    votes = [fn(context) for fn in COMMITTEE_MEMBERS]
    synthesis = synthesize_committee(votes)
    return {
        "ok": True,
        "committee_type": context.get("committee_type") or "general",
        "votes": [asdict(v) for v in votes],
        "synthesis": synthesis,
        "future_committees": [
            "Wealth Committee",
            "Options Committee",
            "Institutional Research Committee",
            "Portfolio Review Committee",
        ],
        "created_at_utc": datetime.utcnow().isoformat(),
    }
