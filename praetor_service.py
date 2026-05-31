from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from praetor_context import PraetorContext
from praetor_providers import AIProvider, AIResult, get_ai_provider


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


def _fmt(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


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


def build_trade_plan(scanner_row: dict[str, Any], style: str = "balanced") -> dict[str, Any]:
    style = (style or "balanced").lower()
    price = _num(scanner_row.get("close"))
    trigger = _num(scanner_row.get("trigger"), price)
    stop = _num(scanner_row.get("stop"), price * 0.92 if price else 0)
    vwap = _num(scanner_row.get("vwap"), price)
    risk_per_share = max(trigger - stop, trigger * 0.02, 0.0001)

    multiplier = {"aggressive": 0.6, "balanced": 1.0, "conservative": 1.35}.get(style, 1.0)
    entry_low = max(stop, trigger - risk_per_share * 0.30 * multiplier)
    entry_high = trigger + risk_per_share * 0.10
    chase_threshold = trigger + risk_per_share * (0.65 if style == "aggressive" else 0.45)

    target_1 = trigger + risk_per_share * 1.5
    target_2 = trigger + risk_per_share * 2.25
    target_3 = trigger + risk_per_share * 3.0
    rr = (target_2 - trigger) / risk_per_share if risk_per_share else None

    intel = scanner_row.get("intelligence") or {}
    confidence = _num(intel.get("confluence_score"), _num(scanner_row.get("confidence"), 50))
    conviction = max(0, min(100, confidence - (10 if "High" in str(intel.get("risk_flag")) else 0)))

    return {
        "id": str(uuid.uuid4()),
        "ticker": scanner_row.get("ticker"),
        "plan_style": style,
        "setup_type": intel.get("setup_type") or scanner_row.get("subtype"),
        "entry_zone_low": entry_low,
        "entry_zone_high": entry_high,
        "trigger_price": trigger,
        "chase_threshold": chase_threshold,
        "stop_price": stop,
        "target_1": target_1,
        "target_2": target_2,
        "target_3": target_3,
        "risk_reward": rr,
        "confidence": confidence,
        "conviction": conviction,
        "valid_conditions": [
            "Relative volume remains elevated.",
            "Liquidity/spread remain tradable.",
            "Structure holds above invalidation.",
            "Price is not materially beyond chase threshold.",
        ],
        "invalidation_conditions": [
            "Price loses stop/invalidation zone.",
            "Relative volume fades materially.",
            "Structure breaks or reclaim fails.",
            "Reward/risk deteriorates from chasing.",
        ],
        "notes": [
            f"VWAP reference: {_fmt(vwap, 4)}.",
            "Confirm liquidity and spread before entry.",
            "This is a plan framework, not an instruction to buy or sell.",
        ],
    }


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
        plan = build_trade_plan(scanner_row, style=style)
        response = (
            f"{plan['ticker']} {plan['plan_style'].title()} Plan: "
            f"entry zone {_fmt(plan['entry_zone_low'], 4)}-{_fmt(plan['entry_zone_high'], 4)}, "
            f"trigger {_fmt(plan['trigger_price'], 4)}, stop {_fmt(plan['stop_price'], 4)}, "
            f"targets {_fmt(plan['target_1'], 4)} / {_fmt(plan['target_2'], 4)} / {_fmt(plan['target_3'], 4)}. "
            "Confirm liquidity, spread, and structure before acting."
        )
        return PraetorResponse(
            ok=True,
            mode="trade_plan",
            response=response,
            provider="deterministic",
            model="trade-plan-v1",
            structured={"trade_plan": plan},
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
