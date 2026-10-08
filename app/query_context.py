"""Bounded, per-browser-tab query context for explicit follow-up questions."""

from __future__ import annotations

import re
from typing import Any

from agents.supervisor_agent import SUPPORTED_CITIES, SUPPORTED_INDUSTRIES


MAX_QUESTION_LENGTH = 240
FOLLOW_UP = re.compile(r"^(那|这个|这些|刚才|上面|同类|还有|再来|继续)")
SALES_REFERENCE = re.compile(r"(这个|这种|刚才|上面|他|她|他们|再|继续)")


def rewrite_query(mode: str, question: str, context: dict[str, Any] | None) -> str:
    """Resolve clear references only within the same window; never reuse profile data."""

    cleaned = question.strip()
    if not isinstance(context, dict) or context.get("mode") != mode or len(cleaned) > MAX_QUESTION_LENGTH:
        return cleaned
    previous = context.get("question")
    if not isinstance(previous, str) or not previous or len(previous) > MAX_QUESTION_LENGTH:
        return cleaned
    if mode == "历史合作案例" and FOLLOW_UP.search(cleaned):
        cities = [city for city in SUPPORTED_CITIES if city in cleaned]
        industries = [industry for industry in SUPPORTED_INDUSTRIES if industry in cleaned]
        if len(cities) > 1 or len(industries) > 1:
            return cleaned
        # A named entity outside our vocabulary should be clarified, not silently
        # replaced with the previous search filter.
        if re.match(r"^那(.+?)呢[？?]?$", cleaned) and not cities and not industries:
            raise ValueError("请明确要继续查询的城市或赛道。")
        city = cities[0] if cities else context.get("city")
        industry = industries[0] if industries else context.get("industry")
        if city or industry:
            return f"查询{city or ''}{industry or ''}历史合作案例。{cleaned}"
    if mode == "销售话术" and SALES_REFERENCE.search(cleaned) and len(cleaned) <= 80:
        situation = previous.removeprefix("客户情况：").split("。当前追问：", 1)[0]
        return f"客户情况：{situation}。当前追问：{cleaned}"
    return cleaned


def next_context(
    mode: str, question: str, status: str, filters: dict[str, str | None] | None,
    previous: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Keep one successful turn; failed requests never become context."""

    if not status.startswith("completed｜"):
        return previous if isinstance(previous, dict) and previous.get("mode") == mode else None
    if mode not in ("历史合作案例", "销售话术"):
        return None
    return {
        "mode": mode,
        "question": question.strip()[:MAX_QUESTION_LENGTH],
        "city": filters.get("city") if filters else None,
        "industry": filters.get("industry") if filters else None,
    }
