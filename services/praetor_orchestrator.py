from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from command_center_engine import build_command_center
from briefing_engine import build_briefing
from committee_engine import run_investment_committee
from monitor_scheduler import build_monitoring_health, run_monitoring_cycle


@dataclass
class PraetorRepositories:
    trade_plan_repo: Any
    alert_repo: Any
    memory_repo: Any
    discovery_repo: Any
    briefing_repo: Any
    committee_repo: Any


@dataclass
class PraetorDataLoaders:
    learning: Callable[[str], dict[str, Any]]
    journal: Callable[[str], dict[str, Any]]


class PraetorOrchestrator:
    """Central service layer for cross-module Praetor workflows.

    Phase 4 starts moving orchestration out of `main.py` without breaking
    existing endpoints. Routes can delegate here incrementally.
    """

    def __init__(self, repos: PraetorRepositories, loaders: PraetorDataLoaders):
        self.repos = repos
        self.loaders = loaders

    def build_context(self, user_id: str) -> dict[str, Any]:
        learning = self.loaders.learning(user_id)
        journal = self.loaders.journal(user_id)
        alerts = self.repos.alert_repo.list_alerts(user_id, limit=100)
        discoveries = self.repos.discovery_repo.list_discoveries(user_id, limit=100)
        plans = self.repos.trade_plan_repo.list_plans(user_id, limit=1000)
        memory = self.repos.memory_repo.list_memory(user_id, limit=100)
        briefings = self.repos.briefing_repo.list_briefings(user_id, limit=20)
        committee_runs = self.repos.committee_repo.list_runs(user_id, limit=10)
        return {
            "learning": learning,
            "journal": journal.get("journal"),
            "alerts": alerts,
            "discoveries": discoveries,
            "trade_plans": plans,
            "memory": memory,
            "risk": learning.get("risk"),
            "briefings": briefings,
            "committee_runs": committee_runs,
        }

    def command_center(self, user_id: str) -> dict[str, Any]:
        return build_command_center(self.build_context(user_id))

    def briefing(self, user_id: str, briefing_type: str) -> dict[str, Any]:
        context = self.build_context(user_id)
        briefing = build_briefing(briefing_type, context)
        briefing_id = self.repos.briefing_repo.save_briefing(user_id, briefing, source_context=context)
        return {"ok": True, "briefing": briefing, "briefing_id": briefing_id}

    def committee(self, user_id: str, committee_type: str = "general") -> dict[str, Any]:
        context = self.build_context(user_id)
        context["committee_type"] = committee_type
        committee = run_investment_committee(context)
        committee_id = self.repos.committee_repo.save_run(user_id, committee, source_context=context)
        return {"ok": True, "committee": committee, "committee_id": committee_id}

    def monitoring_health(self, user_id: str) -> dict[str, Any]:
        return build_monitoring_health(
            self.repos.trade_plan_repo.list_plans(user_id, limit=1000),
            self.repos.alert_repo.list_alerts(user_id, limit=100),
        )

    def monitoring_cycle(
        self,
        user_id: str,
        market_prices: dict[str, Any] | None = None,
        notification_preferences: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        context = self.build_context(user_id)
        return run_monitoring_cycle(
            user_id=user_id,
            trade_plans=context["trade_plans"],
            market_prices=market_prices or {},
            alert_repo=self.repos.alert_repo,
            playbook_context=(context.get("learning") or {}).get("stats") or {},
            memory_context=context.get("memory") or [],
            discovery_context=context.get("discoveries") or [],
            risk_context=context.get("risk") or {},
            notification_preferences=notification_preferences or {},
        )
