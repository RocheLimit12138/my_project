"""将专业 Agent 的结构化结果转换为面向销售人员的自然语言答复。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Final

from agents.generation import GENERATION_WARNING, make_note, render_note
from agents.proposal_agent import ProposalResult
from agents.proposal_draft import render_proposal_draft
from agents.sales_script_agent import SalesScriptResult
from agents.search_agent import SearchResult
from agents.project_tracking_agent import ProjectTrackingResult
from agents.closure_report_agent import ClosureReportResult
from app.case_queries import CaseMetric, CaseRecord
from app.deepseek_client import JsonModel, create_deepseek_client


PENDING_REVIEW_MARKERS: Final = (
    "待业务审核",
    "待审核",
    "未审核",
    "待业务确认",
    "待确认",
)
PENDING_REVIEW_WARNING: Final = (
    "部分指标尚待业务审核，对外使用前请先核验，"
    "不能将其描述为已经核实的公司业绩。"
)
MAX_KEY_METRICS: Final = 5


@dataclass(frozen=True, slots=True)
class ResponseResult:
    """答复 Agent 的标准输出，同时保留来源和风险提示。"""

    intent: str
    found: bool
    answer: str
    sources: tuple[str, ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """转换为 API 和编排层可直接序列化的普通结构。"""

        return {
            "intent": self.intent,
            "found": self.found,
            "answer": self.answer,
            "sources": list(self.sources),
            "warnings": list(self.warnings),
        }


def _describe_filters(result: SearchResult) -> str:
    """把实际查询条件转换成简短、明确的中文描述。"""

    filters: list[str] = []
    if result.city is not None:
        filters.append(f"城市“{result.city}”")
    if result.industry is not None:
        filters.append(f"赛道“{result.industry}”")
    return "、".join(filters) if filters else "当前条件"


def _format_decimal(value: Decimal) -> str:
    """使用非科学计数法显示数值，并去掉无意义的末尾零。"""

    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _format_metric_value(
    number: Decimal | None,
    text: str | None,
    unit: str | None,
) -> str:
    """按实际存在的值生成展示文本，不填补缺失数据。"""

    if text is not None:
        return text
    if number is not None:
        return f"{_format_decimal(number)}{unit or ''}"
    return "未提供"


def _format_metric(metric: CaseMetric) -> list[str]:
    """生成一项指标的变化描述，并原样保留业务备注。"""

    before = _format_metric_value(metric.before_value, metric.before_text, metric.unit)
    after = _format_metric_value(metric.after_value, metric.after_text, metric.unit)
    before_period = metric.before_period or "合作前"
    after_period = metric.after_period or "合作后"
    lines = [
        f"- {metric.metric_name}："
        f"{before_period} {before} → {after_period} {after}"
    ]
    if metric.note:
        lines.append(f"  备注：{metric.note}")
    return lines


def _format_case_summary(index: int, case: CaseRecord) -> list[str]:
    """只生成面向销售人员的商家名称和少量关键指标摘要。"""

    lines = [f"{index}. {case.merchant_name}"]
    if not case.metrics:
        lines.append("   关键指标：未提供")
        return lines
    lines.append("   关键指标：")
    for metric in list(case.metrics.values())[:MAX_KEY_METRICS]:
        lines.append(f"   {_format_metric(metric)[0]}")
    if len(case.metrics) > MAX_KEY_METRICS:
        lines.append(f"   其余 {len(case.metrics) - MAX_KEY_METRICS} 项指标可导出查看")
    return lines


def _contains_pending_review_data(cases: list[CaseRecord]) -> bool:
    """根据原始指标备注判断是否需要展示待审核警示。"""

    return any(
        metric.note and any(marker in metric.note for marker in PENDING_REVIEW_MARKERS)
        for case in cases
        for metric in case.metrics.values()
    )


class ResponseAgent:
    """将案例、话术或方案结果组织成可展示的销售答复。"""

    def __init__(self, *, llm: JsonModel | None = None) -> None:
        self._llm = llm

    def run(
        self,
        search_result: SearchResult | SalesScriptResult | ProposalResult | ProjectTrackingResult | ClosureReportResult,
    ) -> ResponseResult:
        """Render the final user-facing response from a specialist result."""

        if isinstance(search_result, (ProjectTrackingResult, ClosureReportResult)):
            return ResponseResult(
                intent=search_result.intent,
                found=search_result.found,
                answer=search_result.answer,
                sources=search_result.sources,
                warnings=search_result.warnings,
            )
        base = self._run_template(search_result)
        # 案例摘要已经是面向用户的最终结果。完整指标、来源和模型分析保留在
        # 结构化结果中，不再把内部分析拼回默认答复。
        if isinstance(search_result, (SearchResult, SalesScriptResult)):
            return base
        if isinstance(search_result, ProposalResult):
            if search_result.generated is None:
                return base
            profile = search_result.merchant_profile
            count = profile.store_count if profile is not None else None
            scope = (
                "门店数量与具体适用范围待确认。"
                if count is None
                else f"据用户提供，适用门店数量为 {count} 家（尚未核验）；具体门店及执行范围请另行确认。"
            )
            guidance = [scope]
            if search_result.missing_fields:
                guidance.append("执行前建议补充：" + "、".join(g.label for g in search_result.missing_fields) + "。")
            guidance.append("具体门店、预算、排期与目标以双方最终确认内容为准。")
            title, _, body = base.answer.partition("\n")
            intro = "**执行前确认**\n\n" + "\n\n".join("> " + line for line in guidance)
            return replace(base, answer=title + "\n\n" + intro + "\n\n" + body)
        if isinstance(search_result, SalesScriptResult):
            sources = {f"S{i}": item.citation for i, item in enumerate(search_result.snippets, 1)}
            analysis = search_result.analysis
        else:
            sources = {f"C{i}": f"历史合作案例库/{case.case_code}" for i, case in enumerate(search_result.cases, 1)}
            analysis = search_result.analysis
        additions: list[str] = []
        if analysis is not None:
            additions.append("**分析与建议**\n\n" + render_note(analysis, sources))
        if self._llm is not None and search_result.found:
            note = make_note(
                self._llm,
                task="response_agent",
                instructions=(
                    "审阅专业 Agent 的结果，给销售人员 1—3 条简短的使用建议或待确认事项。"
                    "不能新增目标数字、预算金额、报价或事实，不改写原始引用，不把模型分析当已核实结论。"
                ),
                payload={"specialist_result": search_result.to_dict(), "source_ids": sources},
                sources=sources,
            )
            additions.append("**使用建议**\n\n" + render_note(note, sources))
        if not additions:
            return base
        answer = "\n\n".join(additions) + "\n\n" + base.answer
        warnings = tuple(dict.fromkeys((*base.warnings, GENERATION_WARNING)))
        if GENERATION_WARNING not in answer:
            answer += "\n\n提示：" + GENERATION_WARNING
        return replace(base, answer=answer, warnings=warnings)

    def _run_template(
        self,
        search_result: SearchResult | SalesScriptResult | ProposalResult,
    ) -> ResponseResult:
        """生成回答，并分别处理空结果和待审核数据。"""

        if isinstance(search_result, SalesScriptResult):
            return self._respond_to_sales_script(search_result)
        if isinstance(search_result, ProposalResult):
            answer, warnings = render_proposal_draft(search_result)
            return ResponseResult(
                intent="proposal", found=search_result.found, answer=answer,
                sources=search_result.sources, warnings=warnings,
            )
        if not isinstance(search_result, SearchResult):
            raise TypeError("search_result 必须是 SearchResult、SalesScriptResult 或 ProposalResult")

        filter_description = _describe_filters(search_result)
        if not search_result.found:
            answer = (
                f"未找到符合{filter_description}的历史合作案例。"
                "可以尝试放宽城市或赛道条件后再次查询。"
            )
            return ResponseResult(
                intent=search_result.intent, found=False, answer=answer,
                sources=(), warnings=(),
            )

        sources = tuple(f"历史合作案例库/{case.case_code}" for case in search_result.cases)
        warnings = (PENDING_REVIEW_WARNING,) if _contains_pending_review_data(search_result.cases) else ()
        lines = [f"共找到 {search_result.count} 个符合{filter_description}的历史合作案例：", ""]
        for index, case in enumerate(search_result.cases, start=1):
            if index > 1:
                lines.append("")
            lines.extend(_format_case_summary(index, case))
        lines.extend(("", "选择商家旁的“查看详细数据”，可逐条查看完整指标和备注。"))
        if warnings:
            lines.extend(("", f"数据提示：{warnings[0]}"))
        return ResponseResult(
            intent=search_result.intent, found=True, answer="\n".join(lines),
            sources=sources, warnings=warnings,
        )

    def _respond_to_sales_script(self, result: SalesScriptResult) -> ResponseResult:
        """只展示可直接使用的话术结果；检索证据留在结构化响应中。"""

        if not result.found:
            lines = ["未检索到合适的回复，请补充客户的具体异议后重试。"]
        elif result.analysis is not None:
            # DeepSeek 生成的建议只取正文，不把来源、source id 或审核字段展示给用户。
            recommendations = [
                point.text for point in result.analysis.points
                if point.kind == "recommendation"
            ]
            points = recommendations or [point.text for point in result.analysis.points]
            lines = ["建议回复：", *[f"- {point}" for point in points]]
        else:
            # 离线模式没有生成模型时，返回检索到的原文作为可复制回复；
            # 完整候选和来源仍通过 sources/snippets 保留。
            lines = ["建议回复：", *[f"- {snippet.text}" for snippet in result.snippets]]
        return ResponseResult(
            intent=result.intent, found=result.found, answer="\n".join(lines),
            sources=result.sources, warnings=result.warnings,
        )

def run_response_agent(
    search_result: SearchResult | SalesScriptResult | ProposalResult | ProjectTrackingResult | ClosureReportResult,
) -> ResponseResult:
    """提供函数式入口，方便总控 Agent 或 LangGraph 节点调用。"""

    return ResponseAgent(llm=create_deepseek_client()).run(search_result)
