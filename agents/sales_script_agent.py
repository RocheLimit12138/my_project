"""销售话术专业 Agent：检索原文及来源，供答复 Agent 或脚本使用。

注入 DeepSeek 时基于候选生成建议，原文单独保留，不建立索引。默认返回向量检索候选；max_distance
仅供评估后配置，未配置时 found 只表示存在候选，不代表相关性已验证。
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Final

from retrieval.sales_script_store import SalesScriptStore, SearchHit
from app.deepseek_client import JsonModel, create_deepseek_client
from agents.generation import GeneratedNote, make_note, GENERATION_WARNING


SALES_SCRIPT_INTENT: Final = "sales_script"
SalesScriptSearcher = Callable[..., list[SearchHit]]
REVIEW_WARNING: Final = "部分话术尚未确认业务审核状态，对外使用前请核实其中的数据、资质和承诺。"
SOURCE_WARNING: Final = "部分片段缺少完整来源信息，请先核对原始文档。"


class SalesScriptRetrievalError(RuntimeError):
    """检索工具不可用或返回无效结果；上层应区别于正常的空结果。"""


@dataclass(frozen=True, slots=True)
class SalesScriptRequest:
    """标准输入；cosine 距离越小越相关，阈值默认不启用。"""

    question: str
    top_k: int = 5
    max_distance: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.question, str):
            raise TypeError("question 必须是字符串")
        if not self.question.strip():
            raise ValueError("question 不能为空")
        if isinstance(self.top_k, bool) or not isinstance(self.top_k, int):
            raise TypeError("top_k 必须是整数")
        if not 1 <= self.top_k <= 20:
            raise ValueError("top_k 必须在 1 到 20 之间")
        if self.max_distance is not None:
            if isinstance(self.max_distance, bool) or not isinstance(self.max_distance, (int, float)):
                raise TypeError("max_distance 必须是数值或 None")
            if not math.isfinite(self.max_distance) or not 0 <= self.max_distance <= 2:
                raise ValueError("max_distance 必须是 0 到 2 之间的有限数值")
        object.__setattr__(self, "question", self.question.strip())

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> SalesScriptRequest:
        """接受总控的普通字典，忽略 trace_id 等与检索无关的字段。"""

        return cls(
            question=values.get("question"), top_k=values.get("top_k", 5),
            max_distance=values.get("max_distance"),
        )


@dataclass(frozen=True, slots=True)
class SalesScriptSnippet:
    """一个候选片段；text 原样保留检索结果，distance 不是置信概率。"""

    text: str
    distance: float | None
    source: str
    section: str
    subsection: str
    heading_path: str
    chunk_index: int | None
    source_hash: str
    review_status: str

    @property
    def citation(self) -> str:
        """提供可定位至文档章节和片段的引用，不补造缺失信息。"""

        parts = [self.source or "来源未提供"]
        heading = self.heading_path or " / ".join(filter(None, (self.section, self.subsection)))
        if heading:
            parts.append(heading)
        if self.chunk_index is not None:
            parts.append(f"片段 {self.chunk_index}")
        return " / ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """转换为可直接 JSON 序列化的原文和引用信息。"""

        return dict(asdict(self), citation=self.citation)


@dataclass(frozen=True, slots=True)
class SalesScriptResult:
    """话术检索结果；正常空结果与检索失败分开处理。"""

    question: str
    top_k: int
    max_distance: float | None
    snippets: tuple[SalesScriptSnippet, ...]
    warnings: tuple[str, ...]
    intent: str = SALES_SCRIPT_INTENT
    analysis: GeneratedNote | None = None

    @property
    def found(self) -> bool:
        """是否存在通过已配置筛选条件的候选。"""

        return bool(self.snippets)

    @property
    def count(self) -> int:
        """实际返回片段数，而非知识库总数。"""

        return len(self.snippets)

    @property
    def sources(self) -> tuple[str, ...]:
        """按检索顺序返回去重后的引用。"""

        return tuple(dict.fromkeys(item.citation for item in self.snippets))

    def to_dict(self) -> dict[str, Any]:
        """提供后续工作流与答复 Agent 的结构化输入。"""

        return {
            "intent": self.intent, "question": self.question, "top_k": self.top_k,
            "max_distance": self.max_distance, "found": self.found, "count": self.count,
            "snippets": [item.to_dict() for item in self.snippets],
            "sources": list(self.sources), "warnings": list(self.warnings),
            **({"analysis": self.analysis.model_dump(mode="json"),
                "analysis_sources": {f"S{i}": item.citation for i, item in enumerate(self.snippets, 1)}}
               if self.analysis is not None else {}),
        }


def _snippet_from_hit(hit: SearchHit) -> SalesScriptSnippet:
    """验证工具输出；元数据缺失保持未知，不默认视为已审核。"""

    if not isinstance(hit, SearchHit) or not isinstance(hit.text, str) or not hit.text.strip():
        raise ValueError("检索结果必须包含非空原文")
    if not isinstance(hit.metadata, Mapping):
        raise TypeError("检索元数据必须是字典")
    if hit.distance is not None and (
        isinstance(hit.distance, bool) or not isinstance(hit.distance, (int, float))
        or not math.isfinite(hit.distance)
    ):
        raise ValueError("检索距离必须是有限数值或 None")

    def text_field(name: str) -> str:
        value = hit.metadata.get(name)
        return value.strip() if isinstance(value, str) else ""

    index = hit.metadata.get("chunk_index")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        index = None
    return SalesScriptSnippet(
        text=hit.text, distance=hit.distance, source=text_field("source"),
        section=text_field("section"), subsection=text_field("subsection"),
        heading_path=text_field("heading_path"), chunk_index=index,
        source_hash=text_field("source_hash"), review_status=text_field("review_status") or "unknown",
    )


class SalesScriptAgent:
    """调用本地话术库，保留候选排序、原文和来源；支持注入检索工具。"""

    def __init__(self, searcher: SalesScriptSearcher | None = None, *, llm: JsonModel | None = None) -> None:
        self._llm = llm
        if searcher is None:
            project_root = Path(__file__).resolve().parents[1]
            store = SalesScriptStore(
                model_path=project_root / "bge-base-zh-v1.5",
                chroma_path=project_root / ".chroma_db",
            )
            searcher = store.search
        self._searcher = searcher

    def run(self, request: SalesScriptRequest | Mapping[str, Any]) -> SalesScriptResult:
        """校验输入、检索并筛选候选；工具异常保留原始 cause 供上层排查。"""

        if isinstance(request, Mapping):
            request = SalesScriptRequest.from_mapping(request)
        if not isinstance(request, SalesScriptRequest):
            raise TypeError("request 必须是 SalesScriptRequest 或字典")
        try:
            hits = self._searcher(request.question, top_k=request.top_k)
            snippets = tuple(_snippet_from_hit(hit) for hit in hits)
        except Exception as exc:
            raise SalesScriptRetrievalError(
                "销售话术检索失败，请检查查询长度、本地模型及索引状态。"
            ) from exc
        if request.max_distance is not None:
            snippets = tuple(
                item for item in snippets
                if item.distance is not None and item.distance <= request.max_distance
            )
        snippets = snippets[:request.top_k]
        warnings: list[str] = []
        # 只有明确的 approved 才代表已审核；缺失和未知状态均保留审核提示。
        if any(item.review_status != "approved" for item in snippets):
            warnings.append(REVIEW_WARNING)
        if any(not item.source or not (item.heading_path or item.section) for item in snippets):
            warnings.append(SOURCE_WARNING)
        result = SalesScriptResult(
            question=request.question, top_k=request.top_k, max_distance=request.max_distance,
            snippets=snippets, warnings=tuple(warnings),
        )
        if self._llm is not None and snippets:
            sources = {f"S{i}": item.citation for i, item in enumerate(snippets, 1)}
            analysis = make_note(
                self._llm, task="sales_script_agent",
                instructions="针对客户具体异议给出可直接使用的建议话术及使用条件。建议与引用原文分开，未经审核的数值、资质、报价和保证不得转化为对客承诺。",
                payload={"question": request.question,
                         "snippets": [{"id": f"S{i}", **item.to_dict()} for i, item in enumerate(snippets, 1)]},
                sources=sources,
            )
            result = replace(result, analysis=analysis, warnings=(*result.warnings, GENERATION_WARNING))
        return result


def run_sales_script_agent(
    question: str, *, top_k: int = 5, max_distance: float | None = None,
    searcher: SalesScriptSearcher | None = None,
) -> SalesScriptResult:
    """函数式调用入口；重复查询建议复用一个 SalesScriptAgent 实例。"""

    return SalesScriptAgent(searcher, llm=create_deepseek_client()).run(SalesScriptRequest(question, top_k, max_distance))
