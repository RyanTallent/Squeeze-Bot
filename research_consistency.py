from __future__ import annotations

import re
from typing import Any


POSITIVE_RECOMMENDATION_TERMS = (
    "strong buy",
    "buy candidate",
    "buy",
    "accumulate",
    "add",
    "increase exposure",
)
HOLD_RECOMMENDATION_TERMS = ("hold", "maintain position", "maintain exposure")
NEGATIVE_RECOMMENDATION_TERMS = ("avoid", "reduce", "trim", "too risky", "watchlist")

AGGRESSIVE_ACTION_TERMS = ("accumulate", "add", "buy", "increase exposure", "deploy")
HOLD_ACTION_TERMS = ("hold existing", "maintain position", "maintain exposure", "hold")
DEFENSIVE_ACTION_TERMS = ("watchlist", "reduce", "trim", "avoid")


def _text(value: Any) -> str:
    return str(value or "").strip()


def deterministic_research_contract(institutional_report: dict[str, Any]) -> dict[str, Any]:
    opinion = institutional_report.get("opinion") or {}
    conviction = institutional_report.get("conviction") or {}
    valuation = institutional_report.get("valuation") or {}
    coverage = institutional_report.get("data_coverage") or {}
    return {
        "final_recommendation": ((opinion.get("final_recommendation") or {}).get("label") or opinion.get("final_stance") or institutional_report.get("verdict") or "n/a"),
        "today_action": opinion.get("what_praetor_would_do_today") or "n/a",
        "buy_hold_avoid_view": opinion.get("buy_hold_avoid_view") or "n/a",
        "conviction": {
            "score": conviction.get("score"),
            "rating": conviction.get("rating"),
            "confidence": conviction.get("confidence"),
        },
        "valuation": {
            "rating": valuation.get("rating"),
            "score": valuation.get("score"),
            "items": (valuation.get("items") or [])[:4],
        },
        "data_coverage": {
            "score": coverage.get("score"),
            "rating": coverage.get("rating"),
            "missing_data": coverage.get("missing_data") or [],
        },
        "score_conflicts": opinion.get("score_conflicts") or [],
        "opportunity_ranking": opinion.get("opportunity_ranking") or [],
        "highest_conviction_bull_argument": opinion.get("highest_conviction_bull_argument"),
        "highest_conviction_bear_argument": opinion.get("highest_conviction_bear_argument"),
    }


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{re.escape(term)}\b", text) for term in terms)


def _expected_recommendation_family(recommendation: str) -> str:
    rec = recommendation.lower()
    if "avoid" in rec or "reduce" in rec or "too risky" in rec:
        return "defensive"
    if "hold" in rec or "watchlist" in rec:
        return "neutral"
    if "buy" in rec:
        return "positive"
    return "unknown"


def _expected_action_family(action: str) -> str:
    value = action.lower()
    if "accumulate" in value or "buy" in value:
        return "positive"
    if "hold" in value:
        return "neutral"
    if "watchlist" in value or "reduce" in value:
        return "defensive"
    return "unknown"


def _extract_ai_recommendation(text: str) -> str:
    lower = text.lower()
    if "strong buy" in lower:
        return "Strong Buy Candidate"
    if "buy candidate" in lower or re.search(r"\bbuy\b", lower):
        return "Buy Candidate"
    if "reduce" in lower or "trim" in lower:
        return "Reduce / Too Risky"
    if "avoid" in lower:
        return "Avoid for Now"
    if "hold" in lower or "maintain position" in lower or "watchlist" in lower:
        return "Hold / Watchlist"
    return "Not detected"


def _extract_ai_action(text: str) -> str:
    lower = text.lower()
    if "accumulate" in lower:
        return "Accumulate"
    if "buy on pullback" in lower or "buy on pullbacks" in lower:
        return "Buy on Pullbacks"
    if "reduce exposure" in lower or "trim" in lower:
        return "Reduce Exposure"
    if "maintain position" in lower or "hold existing" in lower or re.search(r"\bhold\b", lower):
        return "Hold Existing Position"
    if "watchlist" in lower:
        return "Watchlist Only"
    return "Not detected"


def check_research_ai_consistency(ai_text: str, deterministic_contract: dict[str, Any]) -> dict[str, Any]:
    text = _text(ai_text)
    lower = text.lower()
    deterministic_rec = _text(deterministic_contract.get("final_recommendation"))
    deterministic_action = _text(deterministic_contract.get("today_action"))
    expected_rec = _expected_recommendation_family(deterministic_rec)
    expected_action = _expected_action_family(deterministic_action)
    issues: list[str] = []

    recommendation_consistent = True
    if expected_rec == "defensive" and (_contains_any(lower, POSITIVE_RECOMMENDATION_TERMS) or _contains_any(lower, HOLD_RECOMMENDATION_TERMS)):
        recommendation_consistent = False
        issues.append(f"AI recommendation appears more constructive than deterministic recommendation '{deterministic_rec}'.")
    elif expected_rec == "neutral" and _contains_any(lower, POSITIVE_RECOMMENDATION_TERMS):
        recommendation_consistent = False
        issues.append(f"AI recommendation appears more aggressive than deterministic recommendation '{deterministic_rec}'.")
    elif expected_rec == "positive" and _contains_any(lower, ("avoid", "reduce", "trim")) and "risk" not in lower:
        recommendation_consistent = False
        issues.append(f"AI recommendation appears more negative than deterministic recommendation '{deterministic_rec}'.")

    action_consistent = True
    if expected_action == "defensive" and (_contains_any(lower, AGGRESSIVE_ACTION_TERMS) or _contains_any(lower, HOLD_ACTION_TERMS)):
        action_consistent = False
        issues.append(f"AI action appears more aggressive than deterministic today action '{deterministic_action}'.")
    elif expected_action == "neutral" and _contains_any(lower, AGGRESSIVE_ACTION_TERMS):
        action_consistent = False
        issues.append(f"AI action appears more aggressive than deterministic today action '{deterministic_action}'.")

    coverage = deterministic_contract.get("data_coverage") or {}
    missing = coverage.get("missing_data") or []
    coverage_score = coverage.get("score")
    coverage_consistent = True
    if missing or (coverage_score is not None and float(coverage_score) < 70):
        if "incomplete" not in lower and "missing" not in lower and "coverage" not in lower:
            coverage_consistent = False
            issues.append("AI thesis does not acknowledge deterministic missing/limited data coverage.")
    elif coverage_score is not None and float(coverage_score) >= 85:
        incomplete_phrases = ("data is incomplete", "incomplete data", "limited data", "missing data")
        if any(phrase in lower for phrase in incomplete_phrases):
            coverage_consistent = False
            issues.append("AI thesis says data is incomplete despite strong deterministic data coverage.")

    return {
        "recommendation_consistent": recommendation_consistent,
        "action_consistent": action_consistent,
        "coverage_consistent": coverage_consistent,
        "overall_consistent": recommendation_consistent and action_consistent and coverage_consistent,
        "deterministic_recommendation": deterministic_rec,
        "deterministic_action": deterministic_action,
        "ai_recommendation_detected": _extract_ai_recommendation(text),
        "ai_action_detected": _extract_ai_action(text),
        "issues": issues,
    }


def enforce_research_ai_consistency(ai_result: dict[str, Any], deterministic_contract: dict[str, Any]) -> dict[str, Any]:
    result = dict(ai_result or {})
    text = result.get("text") or ""
    consistency = check_research_ai_consistency(text, deterministic_contract)
    if not consistency["overall_consistent"]:
        anchor = (
            "Praetor consistency correction: deterministic research engines remain the source of truth.\n\n"
            f"Final Recommendation: {deterministic_contract.get('final_recommendation')}\n"
            f"What Praetor Would Do Today: {deterministic_contract.get('today_action')}\n"
            f"Conviction: {(deterministic_contract.get('conviction') or {}).get('score')} "
            f"({(deterministic_contract.get('conviction') or {}).get('rating')})\n"
            f"Valuation: {(deterministic_contract.get('valuation') or {}).get('rating')} "
            f"({(deterministic_contract.get('valuation') or {}).get('score')}/100)\n"
            f"Data Coverage: {(deterministic_contract.get('data_coverage') or {}).get('score')}/100 "
            f"({(deterministic_contract.get('data_coverage') or {}).get('rating')})\n\n"
            "The AI thesis below should be read only as explanation and nuance, not as an override:\n\n"
        )
        result["text"] = anchor + text
        result["consistency_corrected"] = True
    else:
        result["consistency_corrected"] = False
    result["consistency"] = consistency
    result["deterministic_contract"] = deterministic_contract
    return result
