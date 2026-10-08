"""Typed response contract for merchant inputs and eight chapter evidence sets."""

from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field
from agents.merchant_profile import MerchantProfile
from agents.proposal_sections import ProfileGap
from agents.generation import GeneratedProposal


class ProposalReferenceResponse(BaseModel):
    text: str
    distance: float | None
    source: str
    file_type: str
    locator: str
    heading_path: str
    source_hash: str
    review_status: str
    source_kind: str
    citation: str
    section_heading_match: bool | None = None


class ProposalSectionResponse(BaseModel):
    key: str
    title: str
    query: str
    provided_information: dict[str, Any]
    information_status: Literal["user_provided_unverified"]
    missing_fields: list[ProfileGap]
    references: list[ProposalReferenceResponse]
    found: bool
    count: int
    warnings: list[str]
    retrieval_performed: bool


class ProposalEvidenceResponse(BaseModel):
    """No inferred current-merchant facts are added to this preparation payload."""

    merchant_profile: MerchantProfile
    profile_status: Literal["user_provided_unverified"]
    generated: GeneratedProposal | None = None
    generation_sources: dict[str, str] = Field(default_factory=dict)
    sections: list[ProposalSectionResponse] = Field(min_length=8, max_length=8)
    missing_fields: list[ProfileGap]
