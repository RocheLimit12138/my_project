"""历史合作案例查询与数据库结果聚合。

MySQL 中一个案例会对应多条指标记录。本模块负责精确筛选案例，并把 JOIN 返回的
多行数据整理成“一个案例包含全部指标”的对象，供后续 FastAPI、LangGraph 和
Gradio 复用。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from app.database import open_database_connection


MetricType = Literal["number", "text"]
MAX_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class CaseMetric:
    """一个案例指标，保留数值、文本、周期、单位和备注等完整业务信息。"""

    metric_code: str
    metric_name: str
    metric_type: MetricType
    before_value: Decimal | None
    after_value: Decimal | None
    before_text: str | None
    after_text: str | None
    unit: str | None
    before_period: str | None
    after_period: str | None
    note: str | None


@dataclass(frozen=True, slots=True)
class CaseRecord:
    """一次历史合作及其商家快照和全部指标。"""

    case_id: int
    case_code: str
    merchant_id: int
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
    metrics: dict[str, CaseMetric]

    def to_dict(self) -> dict[str, Any]:
        """转换为可交给 FastAPI 或其他展示层处理的普通字典。"""

        return asdict(self)


def _clean_optional_filter(value: str | None, field_name: str) -> str | None:
    """清理可选筛选条件，并拒绝容易造成误查询的纯空白字符串。"""

    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} 不能是空字符串")
    return cleaned


def _validate_pagination(limit: int, offset: int) -> None:
    """限制单次查询规模，避免接口调用意外拉取整个案例库。"""

    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit 必须是整数")
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise ValueError("offset 必须是整数")
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"limit 必须在 1 到 {MAX_PAGE_SIZE} 之间")
    if offset < 0:
        raise ValueError("offset 不能小于 0")


def _build_search_sql(
    city: str | None,
    industry: str | None,
    case_id: int | None = None,
) -> tuple[str, list[Any]]:
    """生成参数化查询。

    子查询先按案例分页，外层查询再关联指标。若直接在 JOIN 后使用 LIMIT，一个
    案例的七项指标可能只返回前几项，导致业务数据不完整。
    """

    conditions: list[str] = []
    params: list[Any] = []
    if city is not None:
        conditions.append("m_filter.city = %s")
        params.append(city)
    if industry is not None:
        conditions.append("m_filter.industry = %s")
        params.append(industry)
    if case_id is not None:
        conditions.append("c_filter.id = %s")
        params.append(case_id)

    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)

    sql = f"""
        SELECT
            c.id AS case_id,
            c.case_code,
            c.start_date,
            c.end_date,
            c.platform,
            c.case_type,
            c.store_count_at_case,
            c.summary,
            m.id AS merchant_id,
            m.merchant_name,
            m.industry,
            m.city,
            m.store_count AS current_store_count,
            cm.id AS metric_id,
            cm.metric_code,
            cm.metric_name,
            cm.metric_type,
            cm.before_value,
            cm.after_value,
            cm.before_text,
            cm.after_text,
            cm.unit,
            cm.before_period,
            cm.after_period,
            cm.note
        FROM (
            SELECT c_filter.id
            FROM cases AS c_filter
            INNER JOIN merchants AS m_filter
                ON m_filter.id = c_filter.merchant_id
            {where_clause}
            ORDER BY c_filter.start_date DESC, c_filter.id DESC
            LIMIT %s OFFSET %s
        ) AS selected_cases
        INNER JOIN cases AS c ON c.id = selected_cases.id
        INNER JOIN merchants AS m ON m.id = c.merchant_id
        LEFT JOIN case_metrics AS cm ON cm.case_id = c.id
        ORDER BY c.start_date DESC, c.id DESC, cm.id ASC
    """
    return sql, params


def _rows_to_cases(rows: Sequence[Mapping[str, Any]]) -> list[CaseRecord]:
    """按 case_id 聚合 JOIN 结果，同时保持 SQL 返回的案例和指标顺序。"""

    grouped: dict[int, dict[str, Any]] = {}
    for row in rows:
        case_id = int(row["case_id"])
        if case_id not in grouped:
            grouped[case_id] = {
                "case_id": case_id,
                "case_code": row["case_code"],
                "merchant_id": int(row["merchant_id"]),
                "merchant_name": row["merchant_name"],
                "industry": row["industry"],
                "city": row["city"],
                "current_store_count": int(row["current_store_count"]),
                "start_date": row["start_date"],
                "end_date": row["end_date"],
                "platform": row["platform"],
                "case_type": row["case_type"],
                "store_count_at_case": int(row["store_count_at_case"]),
                "summary": row["summary"],
                "metrics": {},
            }

        # LEFT JOIN 允许没有指标的案例仍被返回；此时 metric_id 为 NULL。
        if row["metric_id"] is not None:
            metric_code = row["metric_code"]
            grouped[case_id]["metrics"][metric_code] = CaseMetric(
                metric_code=metric_code,
                metric_name=row["metric_name"],
                metric_type=row["metric_type"],
                before_value=row["before_value"],
                after_value=row["after_value"],
                before_text=row["before_text"],
                after_text=row["after_text"],
                unit=row["unit"],
                before_period=row["before_period"],
                after_period=row["after_period"],
                note=row["note"],
            )

    return [CaseRecord(**case_data) for case_data in grouped.values()]


def search_cases(
    city: str | None = None,
    industry: str | None = None,
    *,
    limit: int = 20,
    offset: int = 0,
    case_id: int | None = None,
    connection: Any | None = None,
) -> list[CaseRecord]:
    """按城市和商家赛道查询历史合作案例。

    两个筛选条件都为空时返回最近的案例。调用方可传入连接以复用事务或进行测试；
    未传入时，本函数自行创建并关闭只读查询连接。所有用户输入均通过 SQL 参数
    传递，不会直接拼接到查询语句中。
    """

    cleaned_city = _clean_optional_filter(city, "city")
    cleaned_industry = _clean_optional_filter(industry, "industry")
    _validate_pagination(limit, offset)
    if case_id is not None and (
        isinstance(case_id, bool) or not isinstance(case_id, int) or case_id <= 0
    ):
        raise ValueError("case_id 必须是正整数")
    sql, params = _build_search_sql(cleaned_city, cleaned_industry, case_id)
    params.extend((limit, offset))

    connection_context = (
        nullcontext(connection) if connection is not None else open_database_connection()
    )
    with connection_context as active_connection:
        with active_connection.cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()

    return _rows_to_cases(rows)


def get_case_by_id(
    case_id: int,
    *,
    connection: Any | None = None,
) -> CaseRecord | None:
    """按内部主键读取一条案例，供用户点击详情时按需加载。"""

    if isinstance(case_id, bool) or not isinstance(case_id, int) or case_id <= 0:
        raise ValueError("case_id 必须是正整数")
    matches = search_cases(limit=1, offset=0, case_id=case_id, connection=connection)
    return matches[0] if matches else None
