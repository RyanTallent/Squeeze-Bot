from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from praetor_context import PraetorContext
from praetor_providers import AIProvider, AIResult, get_ai_provider
from trade_plan_engine import build_trade_plan, build_trade_plan_set, summarize_trade_plan


PRAETOR_SYSTEM_PROMPT = """
You are Praetor, Cardo Praevisio's financial decision-intelligence operating system.
You are not a yes-man. Be blunt, professional, calm, analytical, respectful, and evidence-based.
Improve decision quality. Protect capital. Challenge unsupported assumptions.
Do not guarantee returns, fabricate data, or hype a stock.
When evidence is incomplete, say so clearly.
Use professional analysis first, and include plain-English explanation when useful.
"""


@dataclass
class PraetorResponse:
    ok: bool
    mode: str
    response: str
    provider: str
    model: str
    fallback_used: bool = False
    error: str | None = None
    structured: dict[str, Any] | None = None


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def _row_summary(row: dict[str, Any]) -> dict[str, Any]:
    intel = row.get("intelligence") or {}
    return {
        "ticker": row.get("ticker"),
        "company_name": row.get("company_name"),
        "price": row.get("close"),
        "move_pct": row.get("move_pct"),
        "setup_grade": intel.get("setup_grade"),
        "setup_type": intel.get("setup_type") or row.get("subtype"),
        "confluence_score": intel.get("confluence_score"),
        "risk_flag": intel.get("risk_flag"),
        "trend_alignment": intel.get("trend_alignment"),
        "session_status": intel.get("session_status"),
        "subscores": intel.get("subscores"),
        "strengths": intel.get("strengths"),
        "weaknesses": intel.get("weaknesses"),
        "labels": intel.get("context_labels"),
        "ortex_status": row.get("ortex_status"),
        "catalyst_label": row.get("catalyst_label"),
        "catalyst_summary": row.get("catalyst_summary"),
        "sector": row.get("sector"),
        "industry": row.get("industry"),
    }


def deterministic_scanner_answer(question: str, row: dict[str, Any]) -> str:
    intel = row.get("intelligence") or {}
    strengths = intel.get("strengths") or []
    weaknesses = intel.get("weaknesses") or []
    notes = intel.get("execution_notes") or []
    grade = intel.get("setup_grade") or "n/a"
    setup = intel.get("setup_type") or row.get("subtype") or "setup"
    risk = intel.get("risk_flag") or "risk unclear"
    score = intel.get("confluence_score")

    lines = [
        f"Praetor view: {row.get('ticker')} is a {setup} with grade {grade} and confluence {score if score is not None else 'n/a'}.",
        f"Risk read: {risk}.",
    ]
    if strengths:
        lines.append("Evidence supporting the setup: " + "; ".join(strengths[:4]) + ".")
    if weaknesses:
        lines.append("Main concerns: " + "; ".join(weaknesses[:4]) + ".")
    if notes:
        lines.append("Execution discipline: " + " ".join(notes[:2]))
    lines.append("This is not a buy/sell command. Confirm liquidity, spread, structure, and your position size before acting.")
    return "\n\n".join(lines)


def deterministic_playbook_summary() -> dict[str, Any]:
    return {
        "ok": True,
        "status": "foundation",
        "summary": "Playbook foundation is active. More expectancy and behavioral rules will be learned from tagged trades over time.",
        "rules": [
            "Respect invalidation levels.",
            "Avoid chasing beyond the trade plan chase threshold.",
            "Journal watched, traded, and skipped setups.",
        ],
    }


class PraetorService:
    def __init__(self, provider: AIProvider | None = None):
        self.provider = provider or get_ai_provider()

    def ask(self, question: str, context: PraetorContext) -> PraetorResponse:
        context_dict = context.to_dict()
        prompt = (
            f"User question:\n{question}\n\n"
            f"Evidence/context JSON:\n{json.dumps(context_dict, default=str)[:12000]}\n\n"
            "Answer as Praetor. Be direct, evidence-based, and uncertainty-aware."
        )
        ai_result: AIResult = self.provider.complete(PRAETOR_SYSTEM_PROMPT, prompt, context_dict)
        if ai_result.ok:
            return PraetorResponse(
                ok=True,
                mode=context.module,
                response=ai_result.text,
                provider=ai_result.provider,
                model=ai_result.model,
            )

        fallback = deterministic_scanner_answer(question, context.scanner_row or {}) if context.scanner_row else ai_result.text
        return PraetorResponse(
            ok=True,
            mode=context.module,
            response=fallback,
            provider=ai_result.provider,
            model=ai_result.model,
            fallback_used=True,
            error=ai_result.error,
        )

    def scanner_trade_plan(self, scanner_row: dict[str, Any], style: str = "balanced") -> PraetorResponse:
        if (style or "").lower() == "all":
            plans = build_trade_plan_set(scanner_row)
            response = "Generated aggressive, balanced, and conservative plans. Balanced is the default unless your playbook says otherwise."
            structured = {"trade_plans": plans}
        else:
            plan = build_trade_plan(scanner_row, style=style)
            response = summarize_trade_plan(plan)
            structured = {"trade_plan": plan, "trade_plans": [plan]}
        return PraetorResponse(
            ok=True,
            mode="trade_plan",
            response=response,
            provider="deterministic",
            model="trade-plan-v1",
            structured=structured,
        )

    def playbook_summary(self) -> PraetorResponse:
        summary = deterministic_playbook_summary()
        return PraetorResponse(
            ok=True,
            mode="playbook",
            response=summary["summary"],
            provider="deterministic",
            model="playbook-foundation-v1",
            structured=summary,
        )


def response_to_dict(response: PraetorResponse) -> dict[str, Any]:
    return asdict(response)


def utc_now_iso() -> str:
    return datetime.utcnow().isoformat()
