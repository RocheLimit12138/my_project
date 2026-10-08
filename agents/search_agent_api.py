"""搜索 Agent 的兼容导出入口。"""

from agents.search_agent import (
    SearchAgent,
    SearchRequest,
    SearchResult,
    run_search_agent,
)

__all__ = [
    "SearchAgent",
    "SearchRequest",
    "SearchResult",
    "run_search_agent",
]