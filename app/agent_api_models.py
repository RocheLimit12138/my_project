"""自然语言 Agent 接口的 Pydantic 请求与响应模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from app.api_models import CasePreviewResponse
from agents.merchant_profile import MerchantProfile
from app.proposal_api_models import ProposalEvidenceResponse


class AgentChatRequest(BaseModel):
    """自然语言 Agent 查询请求。"""

    question: str = Field(
        min_length=1,
        max_length=1000,
        description="销售人员提出的自然语言问题",
    )
    merchant_profile: MerchantProfile | None = Field(
        default=None, description="可选商家资料；提供后按八章检索，所有输入均视为待核验资料",
    )
    limit: int = Field(default=20, ge=1, le=100, description="案例查询分页大小")
    offset: int = Field(default=0, ge=0, description="案例查询偏移量")
    top_k: int = Field(default=5, ge=1, le=20, strict=True, description="话术或方案参考数量；提供商家资料时为每章候选数量")
    max_distance: float | None = Field(
        default=None, ge=0, le=2, allow_inf_nan=False, strict=True,
        description="话术或案例文档 cosine 距离上限；默认不筛选，两类检索需分别评估后配置",
    )
    operations_payload: dict[str, Any] | None = Field(
        default=None,
        description=(
            "项目跟踪或合作总结的结构化动作参数；写入动作仍由专业 Agent 和业务校验控制"
        ),
    )
    intent_hint: Literal[
        "sales_script", "proposal", "project_tracking", "closure_report"
    ] | None = Field(
        default=None,
        description="界面已明确选择的专业能力；历史案例查询继续交给总控识别。",
    )

    @field_validator("question")
    @classmethod
    def validate_question(cls, value: str) -> str:
        """清理首尾空白，并拒绝只有空白字符的问题。"""

        cleaned = value.strip()
        if not cleaned:
            raise ValueError("question 不能为空")
        return cleaned


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


class SwitchAccountRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)


class WeeklyReviewRequest(BaseModel):
    decision: Literal["approve", "reject"]
    note: str = Field(default="", max_length=1000)


class ProposalExportRequest(BaseModel):
    """把一次已完成的结构化方案导出为可编辑 PPTX。"""

    format: Literal["pptx"]
    proposal: ProposalEvidenceResponse


class OperationsReportExportRequest(BaseModel):
    """把售中或售后的已生成报告转换为可编辑 PPTX。"""

    format: Literal["pptx"]
    report_type: Literal["weekly_tracking", "closure_report"]
    report_markdown: str = Field(min_length=10, max_length=40000)

    @field_validator("report_markdown")
    @classmethod
    def validate_report_markdown(cls, value: str) -> str:
        """只接受包含标题和正文的完整报告，避免生成空白演示文稿。"""

        cleaned = value.strip()
        if not cleaned.startswith("# ") or "\n## " not in cleaned:
            raise ValueError("请先生成完整的项目报告，再导出 PPTX。")
        return cleaned


class AgentChatResponse(BaseModel):
    """自然语言查询工作流的统一响应。"""

    status: Literal[
        "completed",
        "needs_clarification",
        "not_available",
    ]
    intent: str
    answer: str
    filters: dict[str, str | None]
    sources: list[str]
    warnings: list[str]
    proposal: ProposalEvidenceResponse | None = None
    operations: dict[str, Any] | None = None
    case_results: list[CasePreviewResponse] = Field(default_factory=list)
