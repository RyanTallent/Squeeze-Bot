from __future__ import annotations

from datetime import datetime
from typing import Any


def _confidence_label(confidence: float) -> str:
    if confidence >= 0.75:
        return "high"
    if confidence >= 0.40:
        return "medium"
    return "low"


def _memory_item(
    memory_type: str,
    belief_type: str,
    topic: str,
    statement: str,
    confidence: float,
    evidence_count: int,
    source_module: str = "playbook_engine",
) -> dict[str, Any]:
    return {
        "memory_type": memory_type,
        "belief_type": belief_type,
        "topic": topic,
        "statement": statement,
        "confidence": max(0.0, min(1.0, float(confidence))),
        "confidence_label": _confidence_label(confidence),
        "evidence_count": int(evidence_count),
        "source_module": source_module,
        "created_at_utc": datetime.utcnow().isoformat(),
        "updated_at_utc": datetime.utcnow().isoformat(),
    }


def build_memory_updates(playbook_stats: dict[str, Any]) -> list[dict[str, Any]]:
    memories: list[dict[str, Any]] = []
    sample = int(playbook_stats.get("sample_size") or 0)
    if sample > 0:
        memories.append(
            _memory_item(
                "trade_memory",
                "fact",
                "completed_trade_plans",
                f"User has {sample} completed Praetor trade plan outcome(s).",
                min(0.95, 0.25 + sample * 0.05),
                sample,
            )
        )

    for strength in playbook_stats.get("strengths") or []:
        memories.append(
            _memory_item(
                "behavioral_memory",
                "inference",
                "strength",
                strength["title"] + ". " + strength["description"],
                strength.get("confidence") or 0.2,
                strength.get("evidence_count") or 1,
            )
        )

    for weakness in playbook_stats.get("weaknesses") or []:
        memories.append(
            _memory_item(
                "behavioral_memory",
                "hypothesis" if (weakness.get("confidence") or 0) < 0.55 else "inference",
                "weakness",
                weakness["title"] + ". " + weakness["description"],
                weakness.get("confidence") or 0.2,
                weakness.get("evidence_count") or 1,
            )
        )

    if sample >= 1 and (playbook_stats.get("win_rate") is not None):
        wr = playbook_stats["win_rate"]
        if wr >= 0.65:
            statement = f"User's completed Praetor plans currently show a strong win rate of {wr * 100:.0f}%."
            belief = "inference" if sample >= 3 else "hypothesis"
        elif wr <= 0.40:
            statement = f"User's completed Praetor plans currently show a weak win rate of {wr * 100:.0f}%; risk controls may need tightening."
            belief = "inference" if sample >= 3 else "hypothesis"
        else:
            statement = f"User's completed Praetor plans currently show a mixed win rate of {wr * 100:.0f}%."
            belief = "fact"
        memories.append(
            _memory_item(
                "trade_memory",
                belief,
                "overall_performance",
                statement,
                min(0.9, 0.20 + sample * 0.06),
                sample,
            )
        )

    return memories
