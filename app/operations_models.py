"""售中项目跟踪与售后总结共用的数据契约和校验规则。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping


DIRECTION_ALIASES = {
    "higher": "higher",
    "higher_is_better": "higher",
    "越高越好": "higher",
    "lower": "lower",
    "lower_is_better": "lower",
    "越低越好": "lower",
    "target": "target",
    "close_to_target": "target",
    "接近目标": "target",
}
AGGREGATION_ALIASES = {
    "sum": "sum",
    "累计": "sum",
    "latest": "latest",
    "期末": "latest",
}
PROJECT_STATUSES = {"active", "closing", "completed", "cancelled"}
CLOSURE_STATUSES = {"completed", "partially_completed", "cancelled", "paused"}


def required_text(value: Any, field_name: str, *, max_length: int = 200) -> str:
    """校验必填文本，避免空白值进入数据库和报告。"""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name}不能为空。")
    cleaned = value.strip()
    if len(cleaned) > max_length:
        raise ValueError(f"{field_name}不能超过 {max_length} 个字符。")
    return cleaned


def optional_text(value: Any, field_name: str, *, max_length: int = 4000) -> str:
    """将空值统一为字符串，并限制用户输入长度。"""

    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field_name}必须是文本。")
    cleaned = value.strip()
    if len(cleaned) > max_length:
        raise ValueError(f"{field_name}不能超过 {max_length} 个字符。")
    return cleaned


def decimal_value(value: Any, field_name: str, *, required: bool = False) -> Decimal | None:
    """将界面或数据库中的数值安全转换为 Decimal。"""

    if value is None or value == "":
        if required:
            raise ValueError(f"{field_name}不能为空。")
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name}必须是数值。")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name}必须是数值。") from exc
    if not result.is_finite():
        raise ValueError(f"{field_name}必须是有限数值。")
    return result


def date_value(value: Any, field_name: str, *, required: bool = False) -> date | None:
    """接受 ISO 日期字符串或 date 对象。"""

    if value is None or value == "":
        if required:
            raise ValueError(f"{field_name}不能为空。")
        return None
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ValueError(f"{field_name}必须使用 YYYY-MM-DD 格式。")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"{field_name}必须使用 YYYY-MM-DD 格式。") from exc


@dataclass(frozen=True)
class MetricDefinition:
    """一个项目指标的口径、目标和累计方式。"""

    name: str
    unit: str = ""
    baseline_value: Decimal | None = None
    target_value: Decimal | None = None
    direction: str = "higher"
    aggregation: str = "latest"
    data_definition: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MetricDefinition":
        name = required_text(value.get("name"), "指标名称", max_length=100)
        direction_raw = str(value.get("direction") or "higher").strip()
        aggregation_raw = str(value.get("aggregation") or "latest").strip()
        if direction_raw not in DIRECTION_ALIASES:
            raise ValueError(f"指标“{name}”的方向必须是越高越好、越低越好或接近目标。")
        if aggregation_raw not in AGGREGATION_ALIASES:
            raise ValueError(f"指标“{name}”的聚合方式必须是累计或期末。")
        return cls(
            name=name,
            unit=optional_text(value.get("unit"), "指标单位", max_length=30),
            baseline_value=decimal_value(value.get("baseline_value"), f"{name}基线值"),
            target_value=decimal_value(value.get("target_value"), f"{name}目标值"),
            direction=DIRECTION_ALIASES[direction_raw],
            aggregation=AGGREGATION_ALIASES[aggregation_raw],
            data_definition=optional_text(value.get("data_definition"), "指标口径", max_length=1000),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "unit": self.unit,
            "baseline_value": str(self.baseline_value) if self.baseline_value is not None else None,
            "target_value": str(self.target_value) if self.target_value is not None else None,
            "direction": self.direction,
            "aggregation": self.aggregation,
            "data_definition": self.data_definition,
        }


@dataclass(frozen=True)
class ObservedMetric:
    """某一统计周期或项目结束时录入的实际指标。"""

    name: str
    actual_value: Decimal
    source: str
    note: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ObservedMetric":
        name = required_text(value.get("name"), "指标名称", max_length=100)
        return cls(
            name=name,
            actual_value=decimal_value(value.get("actual_value"), f"{name}实际值", required=True),
            source=required_text(value.get("source"), f"{name}数据来源", max_length=300),
            note=optional_text(value.get("note"), f"{name}备注", max_length=1000),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "actual_value": str(self.actual_value),
            "source": self.source,
            "note": self.note,
        }


def unique_metrics(metrics: Iterable[MetricDefinition | ObservedMetric]) -> tuple:
    """拒绝同一批输入中的重复指标，避免覆盖和歧义。"""

    result = []
    names: set[str] = set()
    for metric in metrics:
        normalized = metric.name.casefold()
        if normalized in names:
            raise ValueError(f"指标名称重复：{metric.name}。")
        names.add(normalized)
        result.append(metric)
    return tuple(result)


@dataclass(frozen=True)
class ProjectCreateInput:
    """创建跟踪项目所需的项目、方案快照和指标定义。"""

    project_name: str
    merchant_name: str
    city: str = ""
    industry: str = ""
    store_scope: str = ""
    owner_name: str = ""
    start_date: date | None = None
    end_date: date | None = None
    notes: str = ""
    proposal_snapshot: dict[str, Any] = field(default_factory=dict)
    proposal_source: str = "manual"
    metrics: tuple[MetricDefinition, ...] = ()

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        proposal_snapshot: Mapping[str, Any] | None,
        metrics: Iterable[Mapping[str, Any]],
    ) -> "ProjectCreateInput":
        start = date_value(value.get("start_date"), "推广开始日期")
        end = date_value(value.get("end_date"), "推广结束日期")
        if start is not None and end is not None and end < start:
            raise ValueError("推广结束日期不能早于开始日期。")
        source = str(value.get("proposal_source") or "manual").strip()
        if source not in {"manual", "generated_proposal"}:
            raise ValueError("方案来源必须是手工录入或系统生成方案。")
        snapshot = dict(proposal_snapshot or {})
        if not isinstance(snapshot, dict):
            raise ValueError("营销方案快照必须是 JSON 对象。")
        return cls(
            project_name=required_text(value.get("project_name"), "项目名称"),
            merchant_name=required_text(value.get("merchant_name"), "商家名称"),
            city=optional_text(value.get("city"), "城市", max_length=50),
            industry=optional_text(value.get("industry"), "行业", max_length=100),
            store_scope=optional_text(value.get("store_scope"), "门店范围", max_length=300),
            owner_name=optional_text(value.get("owner_name"), "负责人", max_length=100),
            start_date=start,
            end_date=end,
            notes=optional_text(value.get("notes"), "项目备注"),
            proposal_snapshot=snapshot,
            proposal_source=source,
            metrics=unique_metrics(MetricDefinition.from_mapping(item) for item in metrics),
        )


@dataclass(frozen=True)
class WeeklyUpdateInput:
    """一周实际数据与执行记录。"""

    project_id: int
    period_start: date
    period_end: date
    metrics: tuple[ObservedMetric, ...]
    completed_items: str = ""
    current_issues: str = ""
    pending_confirmations: str = ""
    next_week_plan: str = ""
    risk_notes: str = ""

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        metrics: Iterable[Mapping[str, Any]],
    ) -> "WeeklyUpdateInput":
        project_id = value.get("project_id")
        if isinstance(project_id, bool) or not isinstance(project_id, int) or project_id <= 0:
            raise ValueError("请选择有效的跟踪项目。")
        start = date_value(value.get("period_start"), "统计开始日期", required=True)
        end = date_value(value.get("period_end"), "统计结束日期", required=True)
        if end < start:
            raise ValueError("统计结束日期不能早于开始日期。")
        parsed_metrics = unique_metrics(ObservedMetric.from_mapping(item) for item in metrics)
        if not parsed_metrics:
            raise ValueError("至少录入一项本周指标。")
        return cls(
            project_id=project_id,
            period_start=start,
            period_end=end,
            metrics=parsed_metrics,
            completed_items=optional_text(value.get("completed_items"), "本周完成事项"),
            current_issues=optional_text(value.get("current_issues"), "当前问题"),
            pending_confirmations=optional_text(value.get("pending_confirmations"), "待确认事项"),
            next_week_plan=optional_text(value.get("next_week_plan"), "下周计划"),
            risk_notes=optional_text(value.get("risk_notes"), "其他风险说明"),
        )


@dataclass(frozen=True)
class ClosureInput:
    """合作结束时补充的信息和最终指标。"""

    project_id: int
    closure_date: date
    final_status: str
    final_metrics: tuple[ObservedMetric, ...]
    completed_items: str = ""
    deliverables: str = ""
    unresolved_items: str = ""
    customer_feedback: str = ""
    continuation_recommendation: str = ""
    next_direction: str = ""
    limitations: str = ""

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        metrics: Iterable[Mapping[str, Any]],
    ) -> "ClosureInput":
        project_id = value.get("project_id")
        if isinstance(project_id, bool) or not isinstance(project_id, int) or project_id <= 0:
            raise ValueError("请选择有效的跟踪项目。")
        final_status = str(value.get("final_status") or "completed").strip()
        if final_status not in CLOSURE_STATUSES:
            raise ValueError("项目最终状态无效。")
        return cls(
            project_id=project_id,
            closure_date=date_value(value.get("closure_date"), "合作结束日期", required=True),
            final_status=final_status,
            final_metrics=unique_metrics(ObservedMetric.from_mapping(item) for item in metrics),
            completed_items=optional_text(value.get("completed_items"), "主要完成事项"),
            deliverables=optional_text(value.get("deliverables"), "代表性交付物"),
            unresolved_items=optional_text(value.get("unresolved_items"), "遗留问题"),
            customer_feedback=optional_text(value.get("customer_feedback"), "客户反馈"),
            continuation_recommendation=optional_text(
                value.get("continuation_recommendation"), "继续合作建议", max_length=1000
            ),
            next_direction=optional_text(value.get("next_direction"), "后续方向"),
            limitations=optional_text(value.get("limitations"), "限制条件"),
        )
