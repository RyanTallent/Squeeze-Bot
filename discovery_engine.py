from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
import uuid


@dataclass
class Discovery:
    id: str
    discovery_type: str
    title: str
    description: str
    confidence: float
    evidence_count: int
    source_module: str
    evidence: dict[str, Any]
    created_at_utc: str


def build_discovery_candidates(playbook_stats: dict[str, Any]) -> list[Discovery]:
    """Minimal discovery interface for future expansion.

    Phase 3 only creates conservative candidates from playbook stats. Later versions
    can add scanner, journal, portfolio, risk, and research discoveries.
    """
    discoveries: list[Discovery] = []
    for setup in playbook_stats.get("best_setups") or []:
        if setup.get("sample_size", 0) >= 3 and (setup.get("expectancy") or 0) > 0:
            discoveries.append(
                Discovery(
                    id=str(uuid.uuid4()),
                    discovery_type="hidden_edge",
                    title=f"Possible edge in {setup['key']}",
                    description=f"{setup['key']} has positive expectancy across {setup['sample_size']} completed plan(s).",
                    confidence=setup.get("confidence") or 0.2,
                    evidence_count=setup.get("sample_size") or 0,
                    source_module="playbook_engine",
                    evidence=setup,
                    created_at_utc=datetime.utcnow().isoformat(),
                )
            )
    for setup in playbook_stats.get("worst_setups") or []:
        if setup.get("sample_size", 0) >= 3 and (setup.get("expectancy") or 0) < 0:
            discoveries.append(
                Discovery(
                    id=str(uuid.uuid4()),
                    discovery_type="hidden_risk",
                    title=f"Possible risk pattern in {setup['key']}",
                    description=f"{setup['key']} has negative expectancy across {setup['sample_size']} completed plan(s).",
                    confidence=setup.get("confidence") or 0.2,
                    evidence_count=setup.get("sample_size") or 0,
                    source_module="playbook_engine",
                    evidence=setup,
                    created_at_utc=datetime.utcnow().isoformat(),
                )
            )
    return discoveries


def build_journal_discovery_candidates(journal_report: dict[str, Any]) -> list[Discovery]:
    discoveries: list[Discovery] = []
    for mistake in journal_report.get("recurring_mistakes") or []:
        if mistake.get("evidence_count", 0) >= 2:
            discoveries.append(
                Discovery(
                    id=str(uuid.uuid4()),
                    discovery_type="behavioral_risk",
                    title=f"Recurring journal mistake: {mistake['title']}",
                    description=mistake.get("description") or mistake["title"],
                    confidence=mistake.get("confidence") or 0.2,
                    evidence_count=mistake.get("evidence_count") or 0,
                    source_module="journal_engine",
                    evidence=mistake,
                    created_at_utc=datetime.utcnow().isoformat(),
                )
            )
    for strength in journal_report.get("recurring_strengths") or []:
        if strength.get("evidence_count", 0) >= 2:
            discoveries.append(
                Discovery(
                    id=str(uuid.uuid4()),
                    discovery_type="behavioral_strength",
                    title=f"Recurring journal strength: {strength['title']}",
                    description=strength.get("description") or strength["title"],
                    confidence=strength.get("confidence") or 0.2,
                    evidence_count=strength.get("evidence_count") or 0,
                    source_module="journal_engine",
                    evidence=strength,
                    created_at_utc=datetime.utcnow().isoformat(),
                )
            )
    return discoveries
