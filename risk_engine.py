from __future__ import annotations

from typing import Any


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def _risk_label(score: float) -> str:
    if score >= 76:
        return "Low Risk"
    if score >= 51:
        return "Medium Risk"
    return "High Risk"


def _intervention_level(score: float, evidence_count: int) -> int:
    if score < 30 and evidence_count >= 5:
        return 4
    if score < 45:
        return 3
    if score < 60:
        return 2
    return 1


def _confidence(evidence_count: int, base: float = 0.25) -> float:
    return max(0.0, min(0.95, base + evidence_count * 0.06))


def analyze_trade_risk(plan: dict[str, Any]) -> dict[str, Any]:
    confidence = _num(plan.get("confidence"), 50)
    conviction = _num(plan.get("conviction"), confidence)
    rr = _num(plan.get("risk_reward"), 0)
    setup_grade = str(plan.get("setup_grade") or "")
    score = 65

    if confidence >= 85:
        score += 10
    elif confidence < 55:
        score -= 15

    if conviction >= 80:
        score += 8
    elif conviction < 50:
        score -= 12

    if rr >= 2:
        score += 8
    elif rr and rr < 1.2:
        score -= 12

    if setup_grade in ("A+", "A"):
        score += 8
    elif setup_grade in ("C", "D"):
        score -= 10

    score = max(0, min(100, score))
    evidence = [
        f"Confidence score: {confidence:.0f}",
        f"Conviction score: {conviction:.0f}",
        f"Risk/reward: {rr:.2f}" if rr else "Risk/reward unavailable",
        f"Setup grade: {setup_grade or 'unavailable'}",
    ]
    warnings = []
    if confidence < 55:
        warnings.append("Confidence is below preferred threshold.")
    if conviction < 50:
        warnings.append("Conviction is weak relative to risk.")
    if rr and rr < 1.2:
        warnings.append("Reward/risk is not attractive enough.")
    if setup_grade in ("C", "D"):
        warnings.append("Setup grade is not strong enough for aggressive execution.")

    return {
        "category": "trade_risk",
        "risk_score": score,
        "risk_label": _risk_label(score),
        "confidence": _confidence(len(evidence), 0.35),
        "intervention_level": _intervention_level(score, len(evidence)),
        "title": "Trade Plan Risk",
        "summary": "Evaluates setup quality, confidence, conviction, and reward/risk.",
        "evidence": evidence,
        "warnings": warnings,
    }


def analyze_behavioral_risk(playbook_stats: dict[str, Any], memory_items: list[dict[str, Any]], discoveries: list[dict[str, Any]]) -> dict[str, Any]:
    sample = int(playbook_stats.get("sample_size") or 0)
    win_rate = playbook_stats.get("win_rate")
    expectancy = playbook_stats.get("expectancy")
    score = 72
    evidence_count = sample
    warnings: list[str] = []
    evidence: list[str] = []

    if win_rate is not None:
        evidence.append(f"Completed plan win rate: {win_rate * 100:.0f}%")
        if win_rate < 0.4:
            score -= 22
            warnings.append("Completed plan win rate is weak; reduce risk until quality improves.")
    if expectancy is not None:
        evidence.append(f"Expectancy proxy: {expectancy:.2f}")
        if expectancy < 0:
            score -= 18
            warnings.append("Expectancy proxy is negative.")

    weak_memories = [m for m in memory_items if (m.get("topic") == "weakness" or m.get("belief_type") == "hypothesis")]
    if weak_memories:
        score -= min(20, len(weak_memories) * 6)
        evidence.append(f"{len(weak_memories)} weakness/hypothesis memory item(s) detected.")
        warnings.extend([m.get("statement", "") for m in weak_memories[:3] if m.get("statement")])

    hidden_risks = [d for d in discoveries if d.get("discovery_type") == "hidden_risk"]
    if hidden_risks:
        score -= min(20, len(hidden_risks) * 8)
        evidence.append(f"{len(hidden_risks)} hidden-risk discovery candidate(s).")
        warnings.extend([d.get("title", "") for d in hidden_risks[:3] if d.get("title")])

    score = max(0, min(100, score))
    return {
        "category": "behavioral_risk",
        "risk_score": score,
        "risk_label": _risk_label(score),
        "confidence": _confidence(max(evidence_count, len(evidence)), 0.20),
        "intervention_level": _intervention_level(score, max(evidence_count, len(evidence))),
        "title": "Behavioral Risk",
        "summary": "Evaluates chasing, overconfidence, repeated mistakes, and weak historical patterns from memory/playbook evidence.",
        "evidence": evidence or ["Not enough completed outcomes for behavioral risk confidence."],
        "warnings": warnings or ["No major recurring behavioral risk detected yet."],
    }


def analyze_portfolio_risk_foundation(plans: list[dict[str, Any]]) -> dict[str, Any]:
    tickers = [p.get("ticker") for p in plans if p.get("ticker") and p.get("status") in ("ACTIVE", "TRIGGERED")]
    active_count = len(tickers)
    unique_tickers = len(set(tickers))
    score = 78
    warnings: list[str] = []
    evidence = [f"Active/triggered plans: {active_count}", f"Unique active tickers: {unique_tickers}"]

    if active_count >= 8:
        score -= 20
        warnings.append("Many active plans may create attention and concentration risk.")
    if active_count and unique_tickers < active_count:
        score -= 8
        warnings.append("Multiple active plans reference overlapping tickers.")

    score = max(0, min(100, score))
    return {
        "category": "portfolio_risk_foundation",
        "risk_score": score,
        "risk_label": _risk_label(score),
        "confidence": _confidence(active_count, 0.20),
        "intervention_level": _intervention_level(score, active_count),
        "title": "Portfolio Risk Foundation",
        "summary": "Initial architecture for concentration, sector, and correlation risk. Full portfolio analytics come later.",
        "evidence": evidence,
        "warnings": warnings or ["No major active-plan concentration warning detected yet."],
    }


def build_risk_report(
    plans: list[dict[str, Any]],
    playbook_stats: dict[str, Any],
    memory_items: list[dict[str, Any]],
    discoveries: list[dict[str, Any]],
) -> dict[str, Any]:
    latest_plan = plans[0] if plans else {}
    current_risks = [
        analyze_trade_risk(latest_plan) if latest_plan else {
            "category": "trade_risk",
            "risk_score": 50,
            "risk_label": "Medium Risk",
            "confidence": 0.1,
            "intervention_level": 1,
            "title": "Trade Plan Risk",
            "summary": "No saved trade plan available yet.",
            "evidence": [],
            "warnings": ["Save and complete trade plans to improve risk analysis."],
        },
        analyze_behavioral_risk(playbook_stats, memory_items, discoveries),
        analyze_portfolio_risk_foundation(plans),
    ]

    high_conf_warnings = []
    for risk in current_risks:
        if risk.get("confidence", 0) >= 0.4 or risk.get("risk_label") == "High Risk":
            for warning in risk.get("warnings") or []:
                high_conf_warnings.append(
                    {
                        "warning": warning,
                        "source": risk.get("title"),
                        "confidence": risk.get("confidence"),
                        "intervention_level": risk.get("intervention_level"),
                    }
                )

    overall_score = sum(r["risk_score"] for r in current_risks) / len(current_risks)
    return {
        "ok": True,
        "overall_risk_score": overall_score,
        "overall_risk_label": _risk_label(overall_score),
        "current_risks": current_risks,
        "recurring_risks": current_risks[1].get("warnings") or [],
        "highest_confidence_warnings": high_conf_warnings[:6],
    }
