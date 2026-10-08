"""Validated, user-supplied merchant information for proposal preparation."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Annotated, Any
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

ShortText = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=80)]
Note = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=600)]
ListItem = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=200)]


class MerchantMetric(BaseModel):
    """A reported baseline; period and source stay missing until supplied."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: ShortText
    value: ShortText
    unit: ShortText | None = None
    period: ShortText | None = None
    source: ListItem | None = None


class MerchantProfile(BaseModel):
    """Partial profiles are accepted, never completed with historical facts.

    Strings are bounded, unknown keys are rejected, and numeric values are
    finite and nonnegative. All supplied information remains unverified.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: ShortText | None = None
    industry: ShortText | None = None
    city: ShortText | None = None
    address: ListItem | None = None
    store_count: int | None = Field(default=None, ge=1, le=100000, strict=True)
    store_scope: Note | None = None
    business_scale: Note | None = None
    seat_count: int | None = Field(default=None, ge=0, le=1_000_000, strict=True)
    business_hours: Note | None = None
    data_as_of: ShortText | None = None
    business_stage: ShortText | None = None
    products: Note | None = None
    average_spend_yuan: float | None = Field(default=None, ge=0, le=1e8, strict=True, allow_inf_nan=False)
    business_status: Note | None = None
    problems: tuple[ListItem, ...] = Field(default=(), max_length=10)
    location_notes: Note | None = None
    target_customers: Note | None = None
    baseline_metrics: tuple[MerchantMetric, ...] = Field(default=(), max_length=20)
    goals: tuple[ListItem, ...] = Field(default=(), max_length=10)
    budget_yuan: float | None = Field(default=None, ge=0, le=1e9, strict=True, allow_inf_nan=False)
    campaign_days: int | None = Field(default=None, ge=1, le=3660, strict=True)
    platforms: tuple[ShortText, ...] = Field(default=(), max_length=10)
    constraints: Note | None = None
    measurement_plan: Note | None = None
    data_source: ListItem | None = None

    @field_validator(
        "name", "industry", "city", "address", "store_scope", "business_scale",
        "business_hours", "data_as_of", "business_stage", "products",
        "business_status", "location_notes", "target_customers", "constraints",
        "measurement_plan", "data_source", mode="before",
    )
    @classmethod
    def blank_is_missing(cls, value: object) -> object:
        """A blank optional text field is missing, not a supplied fact."""

        return None if isinstance(value, str) and not value.strip() else value


PROFILE_LABELS = {
    "name": "商家名称", "industry": "品类/赛道", "city": "城市", "address": "具体地址",
    "store_count": "适用门店数量", "store_scope": "适用门店范围",
    "business_scale": "经营规模", "seat_count": "座位数", "business_hours": "营业时段",
    "data_as_of": "资料截至日期", "business_stage": "经营阶段", "products": "主营产品",
    "average_spend_yuan": "客单价（元）", "business_status": "经营现状",
    "problems": "已知问题", "location_notes": "当前门店商圈资料",
    "target_customers": "目标客群", "baseline_metrics": "经营基线",
    "goals": "商家期望目标（非效果承诺）", "budget_yuan": "预算上限（元）",
    "campaign_days": "推广周期（天）", "platforms": "意向平台", "constraints": "执行约束",
    "measurement_plan": "效果衡量与复盘安排", "data_source": "资料来源",
}


FORM_REQUIRED_FIELDS = frozenset({
    # 网页端只要求能够支撑一次方案检索的核心资料。旧版 JSON/API
    # 仍然可以传入更多字段，但不再因为缺少已从网页移除的字段而失败。
    "name", "industry", "city", "address", "products", "problems",
    "target_customers", "goals", "budget_yuan", "campaign_days", "platforms",
})


def _form_lines(value: Any) -> tuple[str, ...]:
    """将表单中的逐行文本转换成去重后的列表字段。"""

    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        raw_values = value
    elif isinstance(value, str):
        raw_values = value.splitlines()
    else:
        raise ValueError("列表字段必须使用逐行文本。")
    result: list[str] = []
    for item in raw_values:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _form_number(value: Any, field_name: str, *, integer: bool = False) -> int | float | None:
    """适配 Gradio Number 返回的 int/float，同时拒绝 NaN 和无穷值。"""

    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name}必须是数值。")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name}必须是数值。") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field_name}必须是有限数值。")
    if integer:
        if not number.is_integer():
            raise ValueError(f"{field_name}必须是整数。")
        return int(number)
    return number


def _form_cell_text(value: Any) -> str | None:
    """将 Gradio 表格单元格转成文本，并把 NaN/空白视为空值。

    Gradio 的 ``Dataframe(type="array")`` 在用户没有填写的单元格中，
    可能返回 ``None``、空字符串或 NumPy 的 ``nan``。不能直接调用
    ``str``，否则空单元格会被错误地提交成字面量 ``"nan"``。
    """

    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    try:
        if math.isnan(value):
            return None
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return text or None


def merchant_profile_from_form(values: Mapping[str, Any]) -> MerchantProfile:
    """把网页表单转换成既有 MerchantProfile 契约。

    前端不接触 JSON；列表使用逐行文本，基线指标使用二维表。这里集中完成
    空值、数字、列表和指标行转换，并对网页标为“必填”的核心资料做校验。
    """

    data: dict[str, Any] = {}
    text_fields = (
        "name", "industry", "city", "address", "store_scope", "business_scale",
        "business_hours", "data_as_of", "business_stage", "products",
        "business_status", "location_notes", "target_customers", "constraints",
        "measurement_plan", "data_source",
    )
    for field_name in text_fields:
        value = values.get(field_name)
        if isinstance(value, str):
            value = value.strip()
        if value not in (None, ""):
            data[field_name] = value

    for field_name, integer in (
        ("store_count", True), ("seat_count", True), ("campaign_days", True),
        ("average_spend_yuan", False), ("budget_yuan", False),
    ):
        number = _form_number(values.get(field_name), PROFILE_LABELS[field_name], integer=integer)
        if number is not None:
            data[field_name] = number

    for field_name in ("problems", "goals", "platforms"):
        data[field_name] = _form_lines(values.get(field_name))

    metrics: list[dict[str, str | None]] = []
    rows = values.get("baseline_metrics")
    if rows is None:
        rows = []
    if hasattr(rows, "values") and hasattr(rows.values, "tolist"):
        rows = rows.values.tolist()
    elif hasattr(rows, "tolist") and not isinstance(rows, list):
        rows = rows.tolist()
    if isinstance(rows, tuple):
        rows = list(rows)
    if not isinstance(rows, list):
        raise ValueError("经营基线表格格式无效。")
    for row in rows:
        if isinstance(row, Mapping):
            cells = [row.get(key) for key in ("指标名称", "数值", "单位", "统计周期", "数据来源")]
        elif isinstance(row, (list, tuple)):
            cells = list(row)
        else:
            continue
        cells += [None] * (5 - len(cells))
        name, value, unit, period, source = [_form_cell_text(item) for item in cells[:5]]
        if name is None and value is None:
            continue
        if name is None or value is None:
            raise ValueError("经营基线每一行都必须填写指标名称和数值。")
        metrics.append({"name": name, "value": value, "unit": unit, "period": period, "source": source})
    data["baseline_metrics"] = metrics

    missing = []
    for field_name in FORM_REQUIRED_FIELDS:
        value = data.get(field_name)
        if value in (None, "", (), []):
            missing.append(PROFILE_LABELS[field_name])
    if missing:
        raise ValueError("请补充必填资料：" + "、".join(sorted(set(missing))) + "。")
    return MerchantProfile.model_validate(data)
