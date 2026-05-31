from __future__ import annotations

import json
from typing import Any

from praetor_providers import AIProvider, get_ai_provider


AI_SYNTHESIS_SYSTEM_PROMPT = """
You are Praetor, Cardo Praevisio's synthesis layer.
You do not replace deterministic financial calculations.
Use only the evidence provided in the context.
Do not fabricate data, catalysts, fundamentals, prices, or guarantees.
Be professional, skeptical, uncertainty-aware, and clear.
Your job is to explain, synthesize, coach, challenge, and prioritize.
If data is missing, explicitly say the evidence is incomplete.
"""


def _fallback(kind: str, context: dict[str, Any]) -> str:
    if kind == "research":
        report = context.get("deterministic_report") or {}
        institutional = context.get("institutional_report") or {}
        conviction = institutional.get("conviction") or {}
        opinion = institutional.get("opinion") or {}
        sections = report.get("sections") or []
        first = sections[0].get("body") if sections else "Deterministic research report is available, but AI synthesis is unavailable."
        return (
            "Praetor synthesis unavailable. Deterministic professional judgment:\n\n"
            f"Praetor Final Recommendation: {(opinion.get('final_recommendation') or {}).get('label') or opinion.get('final_stance') or institutional.get('verdict', 'n/a')}\n"
            f"What Praetor Would Do Today: {opinion.get('what_praetor_would_do_today', 'n/a')}\n"
            f"Buy / Hold / Avoid View: {opinion.get('buy_hold_avoid_view', 'n/a')}\n"
            f"Conviction: {conviction.get('score', 'n/a')} ({conviction.get('rating', 'n/a')})\n\n"
            f"{opinion.get('final_stance_body') or first}\n\n"
            f"Strongest bull argument: {opinion.get('highest_conviction_bull_argument', 'n/a')}\n"
            f"Strongest bear argument: {opinion.get('highest_conviction_bear_argument', 'n/a')}\n"
            "Use the metric tables, scenario framework, and risk notes as the source of truth."
        )
    if kind == "committee":
        s = (context.get("committee") or {}).get("synthesis") or {}
        return (
            "Praetor synthesis unavailable. Deterministic committee synthesis:\n\n"
            f"Consensus: {s.get('consensus', 'n/a')}\n"
            f"Recommendation: {s.get('final_recommendation', 'n/a')}\n"
            f"Disagreement: {s.get('disagreement_summary', 'n/a')}"
        )
    if kind == "risk":
        r = context.get("risk") or {}
        return (
            "Praetor synthesis unavailable. Deterministic risk read:\n\n"
            f"Overall risk: {r.get('overall_risk_label', 'n/a')} ({r.get('overall_risk_score', 'n/a')}).\n"
            "Review current risks and high-confidence warnings before adding exposure."
        )
    if kind == "journal":
        j = context.get("journal") or {}
        return (
            "Praetor synthesis unavailable. Deterministic journal read:\n\n"
            f"Execution score: {j.get('execution_score', 'n/a')}\n"
            f"Discipline score: {j.get('discipline_score', 'n/a')}\n"
            f"Process score: {j.get('process_score', 'n/a')}\n"
            "Review recurring mistakes and strengths for coaching."
        )
    if kind == "briefing":
        b = context.get("briefing") or {}
        return (
            "Praetor synthesis unavailable. Deterministic briefing:\n\n"
            f"{b.get('title', 'Briefing')}: {b.get('lead', 'No lead summary available.')}"
        )
    if kind == "command_center":
        cc = context.get("command_center") or {}
        rec = (cc.get("recommendation_of_day") or {}).get("text")
        risk = (cc.get("risk_overview") or {}).get("overall_risk_label")
        return (
            "Praetor synthesis unavailable. Deterministic command-center read:\n\n"
            f"Recommendation: {rec or 'No critical recommendation.'}\n"
            f"Risk posture: {risk or 'n/a'}\n"
            "Review Command Center cards for source evidence."
        )
    return "Praetor synthesis unavailable. Deterministic outputs remain available."


def _prompt(kind: str, context: dict[str, Any]) -> str:
    guidance = {
        "research": (
            "Act like a senior equity analyst, portfolio manager, and investment committee member. "
            "Do not merely summarize. Give a clear professional opinion using these sections: Praetor Final Recommendation, "
            "What Would Praetor Do Today, Buy / Hold / Avoid View, Long-Term Investment View, Trade View, What Would Change My Mind, "
            "Highest Conviction Bull Argument, Highest Conviction Bear Argument, Key Risks, Key Opportunities, "
            "Resolve Score Conflicts, Opportunity Ranking, Confidence Level, and Data Coverage. Use deterministic opinion/conviction/valuation/peer/data coverage as source of truth. "
            "Be decisive when evidence is strong; lower confidence and explain missing data when evidence is weak. "
            "Explain what matters most, what is noise, what investors may be overlooking, where the market may be wrong, "
            "and where the thesis is strongest/weakest."
        ),
        "committee": "Synthesize committee votes, identify disagreement, strongest bull/bear evidence, and a careful final view.",
        "risk": "Explain the risk profile like a risk officer. Challenge dangerous assumptions and identify the highest-priority risk.",
        "journal": "Coach the user from journal evidence. Identify repeated mistakes, strengths, lessons, and next behavior change.",
        "briefing": "Create an executive briefing summary. Prioritize what matters now, what changed, and what the user should review first.",
        "command_center": "Create three concise outputs with these headings: Praetor Recommendation of the Day, Praetor Risk Insight, and Praetor Opportunity Insight. Use the command-center evidence.",
    }.get(kind, "Synthesize the provided deterministic evidence carefully.")
    return (
        f"Task: {guidance}\n\n"
        "Rules:\n"
        "- Deterministic data is source of truth.\n"
        "- Do not invent missing data.\n"
        "- Use probabilistic language.\n"
        "- Include professional explanation and plain-English explanation when useful.\n"
        "- Be willing to disagree if evidence warrants it.\n\n"
        f"Context JSON:\n{json.dumps(context, default=str)[:14000]}"
    )


def synthesize(kind: str, context: dict[str, Any], provider: AIProvider | None = None) -> dict[str, Any]:
    provider = provider or get_ai_provider()
    result = provider.complete(AI_SYNTHESIS_SYSTEM_PROMPT, _prompt(kind, context), context)
    if result.ok:
        return {
            "ok": True,
            "kind": kind,
            "text": result.text,
            "provider": result.provider,
            "model": result.model,
            "fallback_used": False,
            "error": None,
        }
    return {
        "ok": True,
        "kind": kind,
        "text": _fallback(kind, context),
        "provider": result.provider,
        "model": result.model,
        "fallback_used": True,
        "error": result.error,
    }
