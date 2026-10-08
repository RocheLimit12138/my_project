"""Historical case retrieval for proposal workflows.

Returns structured references for the fixed proposal draft template. It does not
invent current-customer facts or generate prices/effect guarantees.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, asdict, replace
import math
from pathlib import Path
from typing import Any

from retrieval.case_document_store import CaseDocumentStore
from retrieval.sales_script_store import SearchHit
from agents.merchant_profile import MerchantProfile
from app.deepseek_client import JsonModel, create_deepseek_client
from agents.generation import (
    GENERATION_WARNING,
    PROPOSAL_PRIORITY_WARNING,
    GeneratedProposal,
    generate_proposal,
    proposal_source_map,
)
from agents.proposal_sections import (
    PROPOSAL_SECTIONS, SECTION_CANDIDATE_MIN, ProfileGap, SectionSpec,
    section_inputs, section_query, has_section_heading,
)


CaseSearcher = Callable[..., list[SearchHit]]


class ProposalRetrievalError(RuntimeError):
    """Document retrieval failure, distinct from a successful empty search."""


@dataclass(frozen=True, slots=True)
class ProposalRequest:
    """Validated query with an optional cosine-distance filter."""

    question: str
    top_k: int = 5
    max_distance: float | None = None
    merchant_profile: MerchantProfile | Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.question, str) or not self.question.strip():
            raise ValueError("question 不能为空")
        if isinstance(self.top_k, bool) or not isinstance(self.top_k, int) or not 1 <= self.top_k <= 20:
            raise ValueError("top_k 必须在 1 到 20 之间")
        if self.max_distance is not None and (
            isinstance(self.max_distance, bool)
            or not isinstance(self.max_distance, (int, float))
            or not math.isfinite(self.max_distance)
            or not 0 <= self.max_distance <= 2
        ):
            raise ValueError("max_distance 必须是 0 到 2 之间的有限数值")
        object.__setattr__(self, "question", self.question.strip())
        if isinstance(self.merchant_profile, Mapping):
            object.__setattr__(self, "merchant_profile", MerchantProfile.model_validate(self.merchant_profile))
        elif self.merchant_profile is not None and not isinstance(self.merchant_profile, MerchantProfile):
            raise TypeError("merchant_profile 必须是 MerchantProfile 或字典")


@dataclass(frozen=True, slots=True)
class ProposalReference:
    """Historical excerpt with original document locator and review state."""

    text: str
    distance: float | None
    source: str
    file_type: str
    locator: str
    heading_path: str
    source_hash: str
    review_status: str
    source_kind: str = "unverified"
    section_heading_match: bool | None = None

    @property
    def citation(self) -> str:
        parts = [self.source or "来源未提供"]
        if self.locator:
            parts.append(self.locator)
        if self.heading_path and self.heading_path != self.locator:
            parts.append(self.heading_path)
        return " / ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return dict(asdict(self), citation=self.citation)


@dataclass(frozen=True, slots=True)
class ProposalSectionResult:
    """Current-merchant inputs and historical candidates for exactly one chapter."""

    key: str
    title: str
    query: str
    provided_information: dict[str, Any]
    missing_fields: tuple[ProfileGap, ...]
    references: tuple[ProposalReference, ...]
    warnings: tuple[str, ...]
    retrieval_performed: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "title": self.title, "query": self.query,
            "provided_information": self.provided_information,
            "information_status": "user_provided_unverified",
            "missing_fields": [gap.to_dict() for gap in self.missing_fields],
            "references": [ref.to_dict() for ref in self.references],
            "found": bool(self.references), "count": len(self.references),
            "warnings": list(self.warnings), "retrieval_performed": self.retrieval_performed,
        }


@dataclass(frozen=True, slots=True)
class ProposalResult:
    """Reference candidates, not confirmed strategies or current-customer facts."""

    question: str
    top_k: int
    references: tuple[ProposalReference, ...]
    warnings: tuple[str, ...]
    max_distance: float | None = None
    retrieval_performed: bool = True
    merchant_profile: MerchantProfile | None = None
    sections: tuple[ProposalSectionResult, ...] = ()
    generated: GeneratedProposal | None = None

    @property
    def found(self) -> bool:
        return bool(self.references)

    @property
    def sources(self) -> tuple[str, ...]:
        """Return unique citations in retrieval order."""

        return tuple(dict.fromkeys(item.citation for item in self.references))

    @property
    def missing_fields(self) -> tuple[ProfileGap, ...]:
        """Deduplicate gaps across chapters in template order."""

        return tuple(dict.fromkeys(gap for section in self.sections for gap in section.missing_fields))

    def evidence_dict(self) -> dict[str, Any]:
        """Return the structured preparation payload used by the chat API."""

        return {
            "merchant_profile": self.merchant_profile.model_dump(mode="json") if self.merchant_profile is not None else None,
            "profile_status": "user_provided_unverified",
            "sections": [section.to_dict() for section in self.sections],
            "missing_fields": [gap.to_dict() for gap in self.missing_fields],
            **({"generated": self.generated.model_dump(mode="json"), "generation_sources": proposal_source_map(self)}
               if self.generated is not None else {}),
        }

    def to_dict(self) -> dict[str, Any]:
        return {"intent": "proposal", "question": self.question, "top_k": self.top_k,
                "max_distance": self.max_distance,
                "retrieval_performed": self.retrieval_performed,
                "found": self.found, "count": len(self.references),
                "references": [item.to_dict() for item in self.references],
                "sources": list(self.sources), "warnings": list(self.warnings),
                **self.evidence_dict()}


def _reference(hit: SearchHit) -> ProposalReference:
    """Reject invalid tool output; absent provenance remains unknown."""

    if not isinstance(hit, SearchHit) or not isinstance(hit.text, str) or not hit.text.strip():
        raise ValueError("检索结果必须包含非空原文")
    if not isinstance(hit.metadata, Mapping):
        raise TypeError("检索元数据必须是字典")
    if hit.distance is not None and (
        isinstance(hit.distance, bool) or not isinstance(hit.distance, (int, float))
        or not math.isfinite(hit.distance)
    ):
        raise ValueError("检索距离必须是有限数值或 None")
    metadata = hit.metadata

    def text_field(name: str) -> str:
        value = metadata.get(name)
        return value.strip() if isinstance(value, str) else ""
    return ProposalReference(
        text=hit.text, distance=hit.distance,
        source=text_field("source"), file_type=text_field("file_type"),
        locator=text_field("locator") or text_field("page_or_slide"),
        heading_path=text_field("heading_path"), source_hash=text_field("source_hash"),
        review_status=text_field("review_status") or "unknown",
        source_kind=text_field("source_kind") or "unverified",
    )


def _reference_warnings(references: tuple[ProposalReference, ...]) -> tuple[str, ...]:
    """Retain provenance warnings regardless of which chapter retrieved a hit."""

    warnings = []
    if any(item.source_kind == "simulated_example" for item in references):
        warnings.append("部分参考来自明确标注的虚构/模拟案例，仅供方案方法参考，不代表真实商家经营数据或合作业绩。")
    if any(item.review_status != "approved" for item in references):
        warnings.append("历史案例内容尚未完成业务审核，只能作为参考，不能直接当作当前客户事实。")
    if any(not item.source or not item.locator for item in references):
        warnings.append("部分历史案例缺少文件名或页码/幻灯片号，引用待补充。")
    return tuple(warnings)


def _make_section(
    profile: MerchantProfile, spec: SectionSpec, references: tuple[ProposalReference, ...],
    *, retrieval_performed: bool = True,
) -> ProposalSectionResult:
    provided, gaps = section_inputs(profile, spec)
    warnings = list(_reference_warnings(references))
    if gaps:
        warnings.append("本节仍有待补充资料，不能用历史案例替代当前商家事实。")
    if retrieval_performed and not references:
        warnings.append("本节未检索到符合当前条件的参考片段。")
    return ProposalSectionResult(
        spec.key, spec.title, section_query(profile, spec), provided, gaps,
        references, tuple(warnings), retrieval_performed,
    )


def build_proposal_outline(request: ProposalRequest) -> ProposalResult:
    """Prepare inputs and missing fields without constructing a retrieval store."""

    profile = request.merchant_profile
    sections = tuple(
        _make_section(profile, spec, (), retrieval_performed=False)
        for spec in PROPOSAL_SECTIONS
    ) if profile is not None else ()
    return ProposalResult(
        request.question, request.top_k, (), (), request.max_distance,
        retrieval_performed=False, merchant_profile=profile, sections=sections,
    )


class ProposalAgent:
    """Retrieve historical passages, optionally once for each required chapter."""

    def __init__(self, searcher: CaseSearcher | None = None, *, llm: JsonModel | None = None) -> None:
        self._llm = llm
        if searcher is None:
            root = Path(__file__).resolve().parents[1]
            store = CaseDocumentStore(model_path=root / "bge-base-zh-v1.5", chroma_path=root / ".chroma_db")
            searcher = store.search
        self._searcher = searcher

    def _retrieve(self, query: str, request: ProposalRequest, *, candidate_k: int | None = None) -> tuple[ProposalReference, ...]:
        """A failed section aborts the request rather than becoming a fake empty hit."""

        try:
            count = candidate_k if candidate_k is not None else request.top_k
            hits = self._searcher(query, top_k=count)
            references = tuple(_reference(hit) for hit in hits[:count])
        except Exception as exc:
            raise ProposalRetrievalError("历史案例文档检索失败，请检查本地模型及案例索引。") from exc
        if request.max_distance is not None:
            references = tuple(item for item in references
                               if item.distance is not None and item.distance <= request.max_distance)
        return references

    def run(self, request: ProposalRequest | Mapping[str, Any]) -> ProposalResult:
        """Keep legacy one-query retrieval; a supplied profile enables eight queries."""

        if isinstance(request, Mapping):
            request = ProposalRequest(
                question=request.get("question"), top_k=request.get("top_k", 5),
                max_distance=request.get("max_distance"), merchant_profile=request.get("merchant_profile"),
            )
        if not isinstance(request, ProposalRequest):
            raise TypeError("request 必须是 ProposalRequest 或字典")
        # Model generation always gets eight chapters, even with entirely missing input.
        if self._llm is not None and request.merchant_profile is None:
            request = replace(request, merchant_profile=MerchantProfile())
        profile = request.merchant_profile
        if profile is None:
            references = self._retrieve(request.question, request)
            return ProposalResult(
                request.question, request.top_k, references, _reference_warnings(references), request.max_distance,
            )

        section_results = []
        for spec in PROPOSAL_SECTIONS:
            candidates = self._retrieve(
                section_query(profile, spec), request, candidate_k=max(SECTION_CANDIDATE_MIN, request.top_k),
            )
            tagged = [replace(ref, section_heading_match=has_section_heading(ref.text, spec)) for ref in candidates]
            # Stable partition retains the retriever order within each group.
            ranked = sorted(tagged, key=lambda ref: not ref.section_heading_match)
            section_results.append(_make_section(profile, spec, tuple(ranked[:request.top_k])))
        sections = tuple(section_results)
        # Each section retains its own distance/order. The aggregate is an ordered
        # union for citations, not a cross-query relevance ranking.
        unique = {}
        for section in sections:
            for ref in section.references:
                unique.setdefault((ref.source, ref.source_hash, ref.locator, ref.text), ref)
        references = tuple(unique.values())
        warnings = list(_reference_warnings(references))
        warnings.append("当前商家资料来自用户输入，尚未核验；各章参考仅为检索候选，未自动形成分析结论。")
        if any(section.missing_fields for section in sections):
            warnings.append("部分商家资料待补充，具体字段见各章节及 missing_fields。")
        result = ProposalResult(
            request.question, request.top_k, references, tuple(warnings), request.max_distance,
            merchant_profile=profile, sections=sections,
        )
        if self._llm is not None:
            draft = generate_proposal(self._llm, result)
            # This old preparation warning no longer describes a generated result.
            preparation_warning = "当前商家资料来自用户输入，尚未核验；各章参考仅为检索候选，未自动形成分析结论。"
            generated_warnings = tuple(w for w in result.warnings if w != preparation_warning)
            result = replace(
                result,
                generated=draft,
                warnings=(*generated_warnings, GENERATION_WARNING, PROPOSAL_PRIORITY_WARNING),
            )
        return result


def run_proposal_agent(
    question: str, *, top_k: int = 5, max_distance: float | None = None,
    merchant_profile: MerchantProfile | Mapping[str, Any] | None = None,
    searcher: CaseSearcher | None = None,
) -> ProposalResult:
    """Function entry point; reuse an agent instance for repeated queries."""

    return ProposalAgent(searcher, llm=create_deepseek_client()).run(ProposalRequest(question, top_k, max_distance, merchant_profile))
