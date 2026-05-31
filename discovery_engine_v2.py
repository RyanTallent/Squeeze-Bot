from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
import uuid


DISCOVERY_CATEGORIES = ("Edge", "Risk", "Behavioral", "Opportunity", "Educational")
PRIORITIES = ("Critical", "High", "Medium", "Low")


@dataclass
class DiscoveryV2:
    id: str
    category: str
    discovery_type: str
    title: str
    description: str
    confidence: float
    evidence_count: int
    impact_score: float
    importance: str
    urgency: str
    priority: str
    source_module: str
    evidence: dict[str, Any]
    created_at_utc: str


def _confidence(sample_size: int) -> float:
    if sample_size >= 30:
        return 0.90
    if sample_size >= 15:
        return 0.75
    if sample_size >= 8:
        return 0.58
    if sample_size >= 3:
        return 0.40
    if sample_size >= 1:
        return 0.22
    return 0.0


def _priority(confidence: float, impact_score: float, urgency: str) -> str:
    if urgency == "Immediate" or (confidence >= 0.75 and impact_score >= 80):
        return "Critical"
    if confidence >= 0.55 and impact_score >= 65:
        return "High"
    if confidence >= 0.30 and impact_score >= 45:
        return "Medium"
    return "Low"


def _importance(impact_score: float) -> str:
    if impact_score >= 80:
        return "Very High"
    if impact_score >= 65:
        return "High"
    if impact_score >= 45:
        return "Medium"
    return "Low"


def _make(
    category: str,
    discovery_type: str,
    title: str,
    description: str,
    confidence: float,
    evidence_count: int,
    impact_score: float,
    urgency: str,
    source_module: str,
    evidence: dict[str, Any],
) -> DiscoveryV2:
    return DiscoveryV2(
        id=str(uuid.uuid4()),
        category=category,
        discovery_type=discovery_type,
        title=title,
        description=description,
        confidence=max(0.0, min(1.0, confidence)),
        evidence_count=evidence_count,
        impact_score=max(0.0, min(100.0, impact_score)),
        importance=_importance(impact_score),
        urgency=urgency,
        priority=_priority(confidence, impact_score, urgency),
        source_module=source_module,
        evidence=evidence,
        created_at_utc=datetime.utcnow().isoformat(),
    )


def _edge_discoveries(playbook_stats: dict[str, Any]) -> list[DiscoveryV2]:
    out: list[DiscoveryV2] = []
    for row in playbook_stats.get("best_setups") or []:
        sample = int(row.get("sample_size") or 0)
        expectancy = row.get("expectancy") or 0
        win_rate = row.get("win_rate") or 0
        if sample >= 2 and expectancy > 0:
            conf = row.get("confidence") or _confidence(sample)
            impact = min(100, 45 + expectancy * 35 + win_rate * 20 + sample)
            out.append(
                _make(
                    "Edge",
                    "highest_expectancy_setup",
                    f"Potential edge: {row['key']}",
                    f"{row['key']} is currently your strongest setup bucket with {win_rate * 100:.0f}% win rate across {sample} completed plan(s).",
                    conf,
                    sample,
                    impact,
                    "Normal",
                    "discovery_engine_v2",
                    row,
                )
            )
    for row in playbook_stats.get("by_sector") or []:
        sample = int(row.get("sample_size") or 0)
        if sample >= 2 and (row.get("expectancy") or 0) > 0:
            conf = row.get("confidence") or _confidence(sample)
            out.append(
                _make(
                    "Edge",
                    "strongest_sector",
                    f"Sector strength: {row['key']}",
                    f"{row['key']} shows positive outcome profile in your completed plan history.",
                    conf,
                    sample,
                    55 + sample * 2,
                    "Normal",
                    "discovery_engine_v2",
                    row,
                )
            )
    for row in playbook_stats.get("by_time_of_day") or []:
        sample = int(row.get("sample_size") or 0)
        if sample >= 2 and (row.get("expectancy") or 0) > 0:
            conf = row.get("confidence") or _confidence(sample)
            out.append(
                _make(
                    "Edge",
                    "strongest_time_of_day",
                    f"Time-of-day edge: {row['key']}",
                    f"{row['key']} currently has the strongest time-of-day profile in your completed plan history.",
                    conf,
                    sample,
                    50 + sample * 2,
                    "Normal",
                    "discovery_engine_v2",
                    row,
                )
            )
    return out


def _risk_discoveries(playbook_stats: dict[str, Any], journal_report: dict[str, Any] | None, risk_report: dict[str, Any] | None) -> list[DiscoveryV2]:
    out: list[DiscoveryV2] = []
    for row in playbook_stats.get("worst_setups") or []:
        sample = int(row.get("sample_size") or 0)
        expectancy = row.get("expectancy") or 0
        if sample >= 2 and expectancy <= 0:
            conf = row.get("confidence") or _confidence(sample)
            out.append(
                _make(
                    "Risk",
                    "weakest_setup",
                    f"Weak setup pattern: {row['key']}",
                    f"{row['key']} is currently your weakest setup bucket with {row.get('win_rate', 0) * 100:.0f}% win rate across {sample} completed plan(s).",
                    conf,
                    sample,
                    min(100, 55 + abs(expectancy) * 30 + sample),
                    "High" if sample >= 5 else "Normal",
                    "discovery_engine_v2",
                    row,
                )
            )
    for row in playbook_stats.get("by_sector") or []:
        sample = int(row.get("sample_size") or 0)
        if sample >= 2 and (row.get("expectancy") or 0) < 0:
            conf = row.get("confidence") or _confidence(sample)
            out.append(
                _make(
                    "Risk",
                    "weakest_sector",
                    f"Sector risk: {row['key']}",
                    f"{row['key']} has negative expectancy in current completed plan history.",
                    conf,
                    sample,
                    55 + sample * 2,
                    "Normal",
                    "discovery_engine_v2",
                    row,
                )
            )
    for mistake in (journal_report or {}).get("recurring_mistakes") or []:
        sample = int(mistake.get("evidence_count") or 0)
        if sample >= 2:
            conf = mistake.get("confidence") or _confidence(sample)
            out.append(
                _make(
                    "Behavioral",
                    "recurring_mistake",
                    f"Recurring mistake: {mistake['title']}",
                    mistake.get("description") or mistake["title"],
                    conf,
                    sample,
                    60 + sample * 4,
                    "High" if sample >= 4 else "Normal",
                    "discovery_engine_v2",
                    mistake,
                )
            )
    for warning in (risk_report or {}).get("highest_confidence_warnings") or []:
        conf = warning.get("confidence") or 0.2
        level = int(warning.get("intervention_level") or 1)
        out.append(
            _make(
                "Risk",
                "risk_warning",
                f"Risk warning: {warning.get('source') or 'Praetor'}",
                warning.get("warning") or "Risk warning detected.",
                conf,
                level,
                min(100, 45 + level * 12 + conf * 25),
                "Immediate" if level >= 4 else "High" if level >= 3 else "Normal",
                "discovery_engine_v2",
                warning,
            )
        )
    return out


def _opportunity_discoveries(playbook_stats: dict[str, Any]) -> list[DiscoveryV2]:
    out: list[DiscoveryV2] = []
    for row in playbook_stats.get("by_setup") or []:
        sample = int(row.get("sample_size") or 0)
        expectancy = row.get("expectancy") or 0
        if 1 <= sample < 3 and expectancy > 0:
            conf = row.get("confidence") or _confidence(sample)
            out.append(
                _make(
                    "Opportunity",
                    "emerging_strength",
                    f"Emerging strength: {row['key']}",
                    f"{row['key']} is early but improving. More data is needed before treating it as a reliable edge.",
                    conf,
                    sample,
                    42 + sample * 8,
                    "Low",
                    "discovery_engine_v2",
                    row,
                )
            )
    skipped_like = [r for r in playbook_stats.get("by_setup") or [] if r.get("sample_size", 0) == 0]
    for row in skipped_like[:3]:
        out.append(
            _make(
                "Opportunity",
                "underutilized_strength",
                f"Underutilized area: {row['key']}",
                "This setup has insufficient completed outcomes. Consider tracking watched/skipped context before acting on it.",
                0.15,
                0,
                30,
                "Low",
                "discovery_engine_v2",
                row,
            )
        )
    return out


def _educational_discoveries(playbook_stats: dict[str, Any]) -> list[DiscoveryV2]:
    out: list[DiscoveryV2] = []
    sample = int(playbook_stats.get("sample_size") or 0)
    if sample < 5:
        out.append(
            _make(
                "Educational",
                "low_sample_size",
                "More evidence needed",
                "Praetor has limited completed trade-plan outcomes. Treat early strengths and weaknesses as hypotheses, not proven rules.",
                0.25,
                sample,
                35,
                "Low",
                "discovery_engine_v2",
                {"sample_size": sample},
            )
        )
    return out


def build_discovery_v2_candidates(
    playbook_stats: dict[str, Any],
    journal_report: dict[str, Any] | None = None,
    risk_report: dict[str, Any] | None = None,
) -> list[DiscoveryV2]:
    discoveries = []
    discoveries.extend(_edge_discoveries(playbook_stats))
    discoveries.extend(_risk_discoveries(playbook_stats, journal_report, risk_report))
    discoveries.extend(_opportunity_discoveries(playbook_stats))
    discoveries.extend(_educational_discoveries(playbook_stats))
    discoveries.sort(key=lambda d: (PRIORITIES.index(d.priority), -d.impact_score, -d.confidence))
    return discoveries


def summarize_discoveries(discoveries: list[dict[str, Any]]) -> dict[str, Any]:
    def by_priority(priority: str) -> list[dict[str, Any]]:
        return [d for d in discoveries if d.get("priority") == priority]

    def by_category(category: str) -> list[dict[str, Any]]:
        return [d for d in discoveries if d.get("category") == category]

    return {
        "critical": by_priority("Critical"),
        "high_impact": by_priority("High"),
        "emerging_hypotheses": [d for d in discoveries if d.get("category") in ("Opportunity", "Educational")],
        "hidden_opportunities": by_category("Opportunity") + by_category("Edge"),
        "hidden_risks": by_category("Risk") + by_category("Behavioral"),
        "all": discoveries,
    }
