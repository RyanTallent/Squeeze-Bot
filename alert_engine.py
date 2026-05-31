from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
import uuid


ALERT_PRIORITIES = ("Critical", "High", "Medium", "Low")
ALERT_CATEGORIES = (
    "Entry Trigger",
    "Target Reached",
    "Stop Warning",
    "Invalidation Warning",
    "Discovery Alert",
    "Risk Alert",
    "Opportunity Alert",
    "Portfolio Alert",
)


@dataclass
class SmartAlert:
    id: str
    category: str
    alert_type: str
    ticker: str
    message: str
    explanation: str
    priority: str
    urgency: str
    importance: str
    confidence: float
    evidence: dict[str, Any]
    source_modules: list[str]
    related_entity_type: str | None
    related_entity_id: str | None
    created_at_utc: str


def _priority(event_type: str, risk_context: dict[str, Any] | None = None) -> str:
    risk_label = ((risk_context or {}).get("overall_risk_label") or "").lower()
    if event_type in ("Stop Warning", "Invalidation Warning") or "high" in risk_label:
        return "Critical"
    if event_type in ("Entry Trigger", "Target Reached", "Risk Alert"):
        return "High"
    if event_type in ("Discovery Alert", "Opportunity Alert"):
        return "Medium"
    return "Low"


def _urgency(priority: str) -> str:
    if priority == "Critical":
        return "immediate"
    if priority == "High":
        return "high"
    if priority == "Medium":
        return "normal"
    return "low"


def _confidence(event: dict[str, Any], risk_context: dict[str, Any] | None = None) -> float:
    base = 0.55
    if event.get("distance_pct") is not None and abs(float(event["distance_pct"])) <= 0.01:
        base += 0.10
    if (risk_context or {}).get("overall_risk_label") == "High Risk":
        base += 0.10
    if event.get("event_type") in ("Stop Warning", "Target Reached"):
        base += 0.15
    return max(0.0, min(0.95, base))


def build_smart_alert(
    event: dict[str, Any],
    playbook_context: dict[str, Any] | None = None,
    memory_context: list[dict[str, Any]] | None = None,
    discovery_context: list[dict[str, Any]] | None = None,
    risk_context: dict[str, Any] | None = None,
) -> SmartAlert:
    event_type = event.get("event_type") or "Manual Alert"
    ticker = (event.get("ticker") or "").upper()
    priority = _priority(event_type, risk_context)
    confidence = _confidence(event, risk_context)

    why = event.get("why") or f"{event_type} condition was detected for {ticker}."
    matters = event.get("why_it_matters") or "This matters because it may affect whether the original trade plan remains valid."
    changed = event.get("what_changed") or f"Current price is {event.get('current_price')} relative to plan level {event.get('level_price')}."
    explanation = f"Why it fired: {why}\nWhy it matters: {matters}\nWhat changed: {changed}"

    message = event.get("message") or f"{event_type}: {ticker} - {why}"
    evidence = {
        "event": event,
        "playbook_context": playbook_context or {},
        "memory_count": len(memory_context or []),
        "discovery_count": len(discovery_context or []),
        "risk_context": risk_context or {},
    }

    return SmartAlert(
        id=str(uuid.uuid4()),
        category=event.get("category") or event_type,
        alert_type=event_type,
        ticker=ticker,
        message=message,
        explanation=explanation,
        priority=priority,
        urgency=_urgency(priority),
        importance="capital_protection" if priority == "Critical" else "trade_execution",
        confidence=confidence,
        evidence=evidence,
        source_modules=["monitoring_engine", "alert_engine", "risk_engine", "playbook_engine"],
        related_entity_type=event.get("related_entity_type") or "trade_plan",
        related_entity_id=event.get("related_entity_id"),
        created_at_utc=datetime.utcnow().isoformat(),
    )


def smart_alert_to_repo_kwargs(alert: SmartAlert) -> dict[str, Any]:
    return {
        "alert_type": alert.alert_type,
        "ticker": alert.ticker,
        "message": alert.message,
        "urgency": alert.urgency,
        "importance": alert.importance,
        "confidence": alert.confidence,
        "related_entity_type": alert.related_entity_type,
        "related_entity_id": alert.related_entity_id,
        "evidence": alert.evidence,
        "category": alert.category,
        "priority": alert.priority,
        "source_modules": alert.source_modules,
        "explanation": alert.explanation,
    }
