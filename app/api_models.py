"""FastAPI 对外请求与响应的数据契约。

查询模块使用 dataclass 表达内部业务对象；API 层使用 Pydantic 模型固定 JSON
字段、数据类型和接口文档。两层分离后，数据库查询实现可以调整，而对外接口仍
保持稳定。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class CaseMetricResponse(BaseModel):
    """一个案例指标的完整前后对比信息。"""

    metric_code: str = Field(description="稳定的指标编码")
    metric_name: str = Field(description="指标展示名称")
    metric_type: Literal["number", "text"] = Field(description="指标值类型")
    before_value: Decimal | None = Field(default=None, description="合作前数值")
    after_value: Decimal | None = Field(default=None, description="合作后数值")
    before_text: str | None = Field(default=None, description="合作前文本")
    after_text: str | None = Field(default=None, description="合作后文本")
    unit: str | None = Field(default=None, description="数值单位")
    before_period: str | None = Field(default=None, description="合作前统计周期")
    after_period: str | None = Field(default=None, description="合作后统计周期")
    note: str | None = Field(default=None, description="数据备注")


class CaseResponse(BaseModel):
    """一个历史合作案例及其商家信息和全部指标。"""

    case_id: int
    case_code: str
    merchant_id: int
    merchant_name: str
    industry: str
    city: str
    current_store_count: int = Field(description="商家当前门店数量")
    start_date: date
    end_date: date | None
    platform: str
    case_type: str
    store_count_at_case: int = Field(description="合作发生时的门店数量")
    summary: str | None
    metrics: dict[str, CaseMetricResponse]


class CaseMetricPreviewResponse(BaseModel):
    """摘要列表中展示的少量指标，完整备注按需单条加载。"""

    metric_name: str
    before_value: Decimal | None = None
    after_value: Decimal | None = None
    before_text: str | None = None
    after_text: str | None = None
    unit: str | None = None
    before_period: str | None = None
    after_period: str | None = None


class CasePreviewResponse(BaseModel):
    """历史案例摘要；不含完整指标详情和内部案例编号。"""

    case_id: int
    merchant_name: str
    industry: str
    city: str
    current_store_count: int
    start_date: date
    end_date: date | None
    platform: str
    case_type: str
    store_count_at_case: int
    summary: str | None
    key_metrics: list[CaseMetricPreviewResponse]


class CaseSearchResponse(BaseModel):
    """案例查询响应；count 表示本页实际返回数量，不代表数据库总数。"""

    count: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    cases: list[CaseResponse]


class HealthResponse(BaseModel):
    """服务进程健康状态，不包含外部数据库连通性检查。"""

    status: Literal["ok"]


class ErrorResponse(BaseModel):
    """由项目主动返回的统一错误结构。"""

    detail: str
