"""历史合作案例搜索 Agent。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Final

from app.case_queries import CaseRecord, search_cases
from app.deepseek_client import JsonModel, create_deepseek_client
from agents.generation import GeneratedNote, make_note

SEARCH_INTENT: Final = "case_search"
CaseSearcher = Callable[..., list[CaseRecord]]


@dataclass(frozen=True, slots=True)
class SearchRequest:
    """历史案例查询任务的标准输入。"""

    city: str | None = None
    industry: str | None = None
    limit: int = 20
    offset: int = 0

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "SearchRequest":
        """从总控 Agent 输出创建请求，并忽略未知通用字段。"""

        return cls(
            city=values.get("city"),
            industry=values.get("industry"),
            limit=values.get("limit", 20),
            offset=values.get("offset", 0),
        )


@dataclass(frozen=True, slots=True)
class SearchResult:
    """搜索 Agent 的标准输出。"""

    intent: str
    city: str | None
    industry: str | None
    limit: int
    offset: int
    count: int
    cases: list[CaseRecord]
    analysis: GeneratedNote | None = None

    @property
    def found(self) -> bool:
        """标记是否找到案例。"""

        return bool(self.cases)

    def to_dict(self) -> dict[str, Any]:
        """转换为答复 Agent、日志或 API 可使用的普通结构。"""

        return {
            "intent": self.intent,
            "filters": {"city": self.city, "industry": self.industry},
            "limit": self.limit,
            "offset": self.offset,
            "count": self.count,
            "found": self.found,
            "cases": [case.to_dict() for case in self.cases],
            **({"analysis": self.analysis.model_dump(mode="json"),
                "analysis_sources": {f"C{i}": f"历史合作案例库/{case.case_code}" for i, case in enumerate(self.cases, 1)}}
               if self.analysis is not None else {}),
        }

class SearchAgent:
    """历史合作案例专业 Agent。"""

    def __init__(self, searcher: CaseSearcher = search_cases, *, llm: JsonModel | None = None) -> None:
        self._searcher = searcher
        self._llm = llm

    def run(
        self,
        request: SearchRequest | Mapping[str, Any],
    ) -> SearchResult:
        """调用案例查询工具；底层错误交给上层统一处理。"""

        normalized = (
            request
            if isinstance(request, SearchRequest)
            else SearchRequest.from_mapping(request)
        )

        cases = self._searcher(
            city=normalized.city,
            industry=normalized.industry,
            limit=normalized.limit,
            offset=normalized.offset,
        )

        result = SearchResult(
            intent=SEARCH_INTENT,
            city=normalized.city,
            industry=normalized.industry,
            limit=normalized.limit,
            offset=normalized.offset,
            count=len(cases),
            cases=cases,
        )
        if self._llm is not None and cases:
            sources = {f"C{i}": f"历史合作案例库/{case.case_code}" for i, case in enumerate(cases, 1)}
            analysis = make_note(
                self._llm, task="search_agent",
                instructions="分析查询条件下案例的参考价值与局限，不推定当前客户情况；指标原文与审核备注保留。不生成 SQL。",
                payload={"filters": {"city": normalized.city, "industry": normalized.industry},
                         "cases": [{"id": f"C{i}", **case.to_dict()} for i, case in enumerate(cases, 1)]},
                sources=sources,
            )
            result = replace(result, analysis=analysis)
        return result


def run_search_agent(
    *,
    city: str | None = None,
    industry: str | None = None,
    limit: int = 20,
    offset: int = 0,
    searcher: CaseSearcher = search_cases,
) -> SearchResult:
    """提供函数式入口，方便总控 Agent 或脚本调用。"""

    return SearchAgent(searcher=searcher, llm=create_deepseek_client()).run(
        SearchRequest(
            city=city,
            industry=industry,
            limit=limit,
            offset=offset,
        )
    )