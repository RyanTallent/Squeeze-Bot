from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

from alert_engine import build_smart_alert, smart_alert_to_repo_kwargs
from monitoring_engine import monitor_trade_plans


DEFAULT_ALERT_COOLDOWN_MINUTES = 20
VALID_NOTIFICATION_LEVELS = ("critical", "high_and_critical", "all")


def utc_now() -> datetime:
    return datetime.utcnow()


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def is_stale_plan(plan: dict[str, Any], max_age_hours: int = 24) -> bool:
    created = parse_dt(plan.get("created_at_utc"))
    if not created:
        return False
    return utc_now() - created > timedelta(hours=max_age_hours)


def alert_fingerprint(user_id: str, event: dict[str, Any]) -> str:
    raw = "|".join(
        [
            user_id,
            str(event.get("event_type") or ""),
            str(event.get("ticker") or ""),
            str(event.get("related_entity_id") or event.get("plan_id") or ""),
            str(event.get("level_price") or ""),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def priority_allowed(priority: str | None, preference_level: str = "high_and_critical") -> bool:
    pref = preference_level if preference_level in VALID_NOTIFICATION_LEVELS else "high_and_critical"
    p = (priority or "Low").title()
    if pref == "all":
        return True
    if pref == "critical":
        return p == "Critical"
    return p in ("Critical", "High")


def build_monitoring_health(plans: list[dict[str, Any]], alerts: list[dict[str, Any]]) -> dict[str, Any]:
    active = [p for p in plans if p.get("status") in ("ACTIVE", "TRIGGERED")]
    stale = [p for p in active if is_stale_plan(p)]
    open_alerts = [a for a in alerts if a.get("status") == "OPEN"]
    return {
        "ok": True,
        "active_plan_count": len(active),
        "stale_plan_count": len(stale),
        "open_alert_count": len(open_alerts),
        "last_evaluation_utc": utc_now().isoformat(),
        "failed_check_count": 0,
        "stale_plans": [
            {"id": p.get("id"), "ticker": p.get("ticker"), "created_at_utc": p.get("created_at_utc"), "status": p.get("status")}
            for p in stale[:25]
        ],
    }


def run_monitoring_cycle(
    user_id: str,
    trade_plans: list[dict[str, Any]],
    market_prices: dict[str, Any],
    alert_repo: Any,
    playbook_context: dict[str, Any],
    memory_context: list[dict[str, Any]],
    discovery_context: list[dict[str, Any]],
    risk_context: dict[str, Any],
    notification_preferences: dict[str, Any] | None = None,
) -> dict[str, Any]:
    prefs = notification_preferences or {}
    cooldown_minutes = int(prefs.get("cooldown_minutes") or DEFAULT_ALERT_COOLDOWN_MINUTES)
    preference_level = prefs.get("alert_level") or "high_and_critical"

    existing_alerts = alert_repo.list_alerts(user_id, limit=250)
    existing_fingerprints = {
        (a.get("evidence") or {}).get("fingerprint"): a
        for a in existing_alerts
        if (a.get("evidence") or {}).get("fingerprint")
    }

    events = monitor_trade_plans(trade_plans, market_prices or {})
    created_alerts = []
    suppressed = []

    for event in events:
        smart = build_smart_alert(
            event,
            playbook_context=playbook_context,
            memory_context=memory_context,
            discovery_context=discovery_context,
            risk_context=risk_context,
        )
        fp = alert_fingerprint(user_id, event)
        smart.evidence["fingerprint"] = fp
        smart.evidence["cooldown_minutes"] = cooldown_minutes

        existing = existing_fingerprints.get(fp)
        if existing:
            created_at = parse_dt(existing.get("created_at_utc"))
            if created_at and utc_now() - created_at < timedelta(minutes=cooldown_minutes):
                suppressed.append({"event": event, "reason": "cooldown", "fingerprint": fp})
                continue

        if not priority_allowed(smart.priority, preference_level):
            suppressed.append({"event": event, "reason": "notification_preference", "fingerprint": fp})
            continue

        alert_id = alert_repo.create_alert(user_id=user_id, **smart_alert_to_repo_kwargs(smart))
        created_alerts.append({**smart.__dict__, "id": alert_id})
        existing_fingerprints[fp] = {"created_at_utc": utc_now().isoformat(), "evidence": {"fingerprint": fp}}

    health = build_monitoring_health(trade_plans, alert_repo.list_alerts(user_id, limit=250))
    return {
        "ok": True,
        "events": events,
        "created_alerts": created_alerts,
        "suppressed": suppressed,
        "alert_count": len(created_alerts),
        "suppressed_count": len(suppressed),
        "health": health,
    }
