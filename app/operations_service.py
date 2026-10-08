"""售中周报和售后合作总结的确定性计算与模板生成。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from app.operations_models import ClosureInput, ProjectCreateInput, WeeklyUpdateInput, decimal_value


CLOSURE_STATUS_LABELS = {
    "completed": "已完成",
    "partially_completed": "部分完成",
    "cancelled": "已取消",
    "paused": "已暂停",
}


def _decimal(value: Any) -> Decimal | None:
    return decimal_value(value, "指标数值")


def _number(value: Decimal | None) -> str:
    if value is None:
        return "待补充"
    normalized = value.normalize()
    text = format(normalized, "f")
    return "0" if text == "-0" else text


def _display(value: Decimal | None, unit: str) -> str:
    return f"{_number(value)}{unit}" if value is not None else "待补充"


def _text(value: Any, fallback: str = "本项尚未补充。") -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback


def _ratio_text(value: Decimal | None) -> str:
    if value is None:
        return "暂无法判断"
    return f"{(value * Decimal('100')).quantize(Decimal('0.1'))}%"


def _change(current: Decimal, previous: Decimal | None) -> tuple[Decimal | None, str]:
    if previous is None:
        return None, "无上周可比数据"
    if previous == 0:
        if current == 0:
            return Decimal("0"), "持平"
        return None, "上周为 0，无法计算百分比"
    ratio = (current - previous) / abs(previous)
    if ratio > 0:
        return ratio, f"上升 {_ratio_text(ratio)}"
    if ratio < 0:
        return ratio, f"下降 {_ratio_text(abs(ratio))}"
    return ratio, "持平"


def _target_result(
    actual: Decimal | None,
    target: Decimal | None,
    direction: str,
) -> tuple[Decimal | None, str]:
    """返回可解释完成率和四档状态，不做业务归因。"""

    if actual is None or target is None:
        return None, "暂无法判断"
    if direction == "higher":
        if target == 0:
            return None, "已达到" if actual >= 0 else "未达到"
        ratio = actual / target
        return ratio, "已达到" if ratio >= 1 else ("部分达到" if ratio >= Decimal("0.8") else "未达到")
    if direction == "lower":
        if actual <= target:
            ratio = Decimal("1") if actual == 0 else target / actual
            return ratio, "已达到"
        if target == 0:
            return Decimal("0"), "未达到"
        ratio = target / actual
        return ratio, "部分达到" if actual <= target * Decimal("1.2") else "未达到"
    tolerance = max(abs(target) * Decimal("0.05"), Decimal("0.000001"))
    gap = abs(actual - target)
    if gap <= tolerance:
        return Decimal("1"), "已达到"
    if gap <= max(abs(target) * Decimal("0.2"), tolerance):
        return Decimal("0.8"), "部分达到"
    return Decimal("0"), "未达到"


def _definition_map(project: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["metric_name"]).casefold(): dict(item) for item in project.get("metrics", [])}


def _update_metric_map(update: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["metric_name"]).casefold(): dict(item) for item in update.get("metrics", [])}


def _aggregate_value(definition: Mapping[str, Any], updates: Iterable[Mapping[str, Any]]) -> Decimal | None:
    values: list[Decimal] = []
    key = str(definition["metric_name"]).casefold()
    for update in updates:
        metric = _update_metric_map(update).get(key)
        if metric is not None:
            value = _decimal(metric.get("actual_value"))
            if value is not None:
                values.append(value)
    if not values:
        return None
    if definition.get("aggregation_method") == "sum":
        return sum(values, Decimal("0"))
    return values[-1]


def _snapshot_summary(snapshot: Mapping[str, Any]) -> str:
    if not snapshot:
        return ""

    def joined(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (list, tuple)):
            return "；".join(item for entry in value if (item := joined(entry)))
        if isinstance(value, Mapping):
            return "；".join(item for entry in value.values() if (item := joined(entry)))
        return str(value) if value is not None else ""

    def money(value: Any) -> str:
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return joined(value)
        rendered = f"{amount:,.2f}".rstrip("0").rstrip(".")
        return f"{rendered} 元"

    lines: list[str] = []
    goals = snapshot.get("goals") or snapshot.get("promotion_goals")
    if text := joined(goals):
        lines.append(f"- 推广目标：{text}")
    budget = snapshot.get("budget_limit")
    if budget is None:
        budget = snapshot.get("budget")
    if budget not in (None, ""):
        lines.append(f"- 预算上限：{money(budget)}")
    period = snapshot.get("period")
    if isinstance(period, Mapping):
        start = joined(period.get("start"))
        end = joined(period.get("end"))
        if start and end:
            lines.append(f"- 执行周期：{start} 至 {end}")
        elif start or end:
            lines.append(f"- 执行周期：{start or end}")
    elif text := joined(period):
        lines.append(f"- 执行周期：{text}")
    if text := joined(snapshot.get("strategies")):
        lines.append(f"- 核心策略：{text}")
    if text := joined(snapshot.get("constraints")):
        lines.append(f"- 执行约束：{text}")
    return "\n".join(lines) or "- 营销方案内容已纳入本次项目评估。"


def _weekly_comparison(
    project: Mapping[str, Any],
    updates: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    current = updates[-1]
    previous = updates[-2] if len(updates) >= 2 else None
    current_map = _update_metric_map(current)
    previous_map = _update_metric_map(previous) if previous else {}
    comparison: list[dict[str, Any]] = []
    for definition in project.get("metrics", []):
        key = str(definition["metric_name"]).casefold()
        current_metric = current_map.get(key)
        previous_metric = previous_map.get(key)
        actual = _decimal(current_metric.get("actual_value")) if current_metric else None
        previous_actual = _decimal(previous_metric.get("actual_value")) if previous_metric else None
        cumulative = _aggregate_value(definition, updates)
        target = _decimal(definition.get("target_value"))
        ratio, status = _target_result(cumulative, target, str(definition.get("target_direction") or "higher"))
        change_ratio, change_text = _change(actual, previous_actual) if actual is not None else (None, "本周缺失")
        comparison.append({
            "metric_name": definition["metric_name"],
            "unit": definition.get("unit") or "",
            "baseline_value": definition.get("baseline_value"),
            "target_value": definition.get("target_value"),
            "current_value": str(actual) if actual is not None else None,
            "previous_value": str(previous_actual) if previous_actual is not None else None,
            "progress_value": str(cumulative) if cumulative is not None else None,
            "change_ratio": str(change_ratio) if change_ratio is not None else None,
            "change_text": change_text,
            "completion_ratio": str(ratio) if ratio is not None else None,
            "target_status": status,
            "data_source": current_metric.get("data_source") if current_metric else None,
            "note": current_metric.get("note") if current_metric else None,
        })
    return comparison


def _issue(
    code: str,
    title: str,
    evidence: str,
    impact: str,
    check: str,
    action: str,
    *,
    evidence_type: str = "data_rule",
) -> dict[str, str]:
    return {
        "code": code,
        "title": title,
        "evidence": evidence,
        "possible_impact": impact,
        "suggested_check": check,
        "suggested_action": action,
        "evidence_type": evidence_type,
    }


def _weekly_issues(
    project: Mapping[str, Any],
    updates: list[Mapping[str, Any]],
    comparison: list[Mapping[str, Any]],
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    current = updates[-1]
    missing = [item["metric_name"] for item in comparison if item.get("current_value") is None]
    if missing:
        issues.append(_issue(
            "missing_metrics", "本周指标不完整", "缺失指标：" + "、".join(missing),
            "无法完整判断项目进度或指标之间的关系。", "确认数据是否尚未回传或统计口径发生变化。",
            "补齐数据来源和实际值后重新生成周报。",
        ))
    no_baseline = [item["metric_name"] for item in project.get("metrics", []) if item.get("baseline_value") is None]
    if no_baseline:
        issues.append(_issue(
            "missing_baseline", "部分目标缺少基线", "缺少基线：" + "、".join(no_baseline),
            "无法计算合作前后变化，目标合理性也难以复核。", "核对营销方案或合作前统计数据。",
            "经业务确认后补充基线，未确认前保留“暂无法判断”。",
        ))

    # 预算或消耗指标仅在存在完整项目周期时进行节奏判断。
    start = date.fromisoformat(project["start_date"]) if project.get("start_date") else None
    end = date.fromisoformat(project["end_date"]) if project.get("end_date") else None
    current_end = date.fromisoformat(str(current["period_end"]))
    if start and end and end > start:
        elapsed = Decimal(str(max(0, min((current_end - start).days, (end - start).days))))
        time_ratio = elapsed / Decimal(str((end - start).days))
        for item in comparison:
            name = str(item["metric_name"])
            if any(token in name for token in ("预算", "花费", "消耗", "投放费用", "成本")):
                actual = _decimal(item.get("progress_value"))
                target = _decimal(item.get("target_value"))
                if actual is not None and target and target > 0:
                    spend_ratio = actual / target
                    if spend_ratio > time_ratio + Decimal("0.15"):
                        issues.append(_issue(
                            "budget_pacing", "预算消耗快于项目进度",
                            f"{name}已使用 {_ratio_text(spend_ratio)}，周期进度约 {_ratio_text(time_ratio)}。",
                            "后续周期可能出现预算不足，影响既定投放节奏。",
                            "核对预算口径、退款/补贴、渠道账单和后续排期。",
                            "在业务确认后调整投放节奏或重新分配剩余预算。",
                        ))

    comparison_map = {str(item["metric_name"]).casefold(): item for item in comparison}
    exposure = next((value for key, value in comparison_map.items() if "曝光" in key), None)
    order = next((value for key, value in comparison_map.items() if "订单" in key or "核销" in key), None)
    if exposure and order:
        exposure_change = _decimal(exposure.get("change_ratio"))
        order_change = _decimal(order.get("change_ratio"))
        if exposure_change is not None and exposure_change >= Decimal("0.1") and (
            order_change is None or order_change <= Decimal("0.05")
        ):
            issues.append(_issue(
                "exposure_conversion_gap", "曝光增长未同步转化为订单增长",
                f"曝光{exposure['change_text']}，订单{order['change_text']}。",
                "流量增长可能没有有效进入转化环节，但当前数据不足以直接判断根因。",
                "核对点击、进店、下单、核销各环节数据以及活动页面和门店承接情况。",
                "先定位漏斗异常环节，再决定是否调整素材、人群、优惠或门店执行。",
            ))

    if len(updates) >= 3:
        for definition in project.get("metrics", []):
            if definition.get("target_direction") != "higher":
                continue
            values = []
            for update in updates[-3:]:
                metric = _update_metric_map(update).get(str(definition["metric_name"]).casefold())
                values.append(_decimal(metric.get("actual_value")) if metric else None)
            if all(value is not None for value in values) and values[0] > values[1] > values[2]:
                issues.append(_issue(
                    "consecutive_decline", f"{definition['metric_name']}连续下降",
                    "最近三期实际值依次为 " + "、".join(_number(value) for value in values),
                    "若趋势持续，可能影响项目目标完成。", "核对统计口径、渠道变化、活动节奏和门店执行记录。",
                    "将该指标列为下周重点观察项，并为确认后的原因制定针对性动作。",
                ))

    if current.get("risk_notes"):
        issues.append(_issue(
            "manual_risk", "补充风险说明", str(current["risk_notes"]),
            "影响程度尚待业务确认。", "确认风险发生范围、时间、证据和责任人。",
            "将确认后的处理动作纳入下周计划。", evidence_type="user_input",
        ))
    return issues


def _weekly_report_markdown(
    project: Mapping[str, Any],
    current: Mapping[str, Any],
    comparison: list[Mapping[str, Any]],
    issues: list[Mapping[str, str]],
) -> str:
    rows = ["| 指标 | 本周实际 | 上周对比 | 项目进度值 | 目标值 | 状态 | 数据来源 |", "|---|---:|---|---:|---:|---|---|"]
    for item in comparison:
        unit = str(item.get("unit") or "")
        rows.append(
            f"| {item['metric_name']} | {_display(_decimal(item.get('current_value')), unit)} | "
            f"{item['change_text']} | {_display(_decimal(item.get('progress_value')), unit)} | "
            f"{_display(_decimal(item.get('target_value')), unit)} | {item['target_status']} | "
            f"{item.get('data_source') or '待补充'} |"
        )
    issue_lines = []
    for item in issues:
        issue_lines.append(
            f"- **{item['title']}**\n"
            f"  - 判断依据：{item['evidence']}\n"
            f"  - 可能影响：{item['possible_impact']}\n"
            f"  - 建议核查：{item['suggested_check']}\n"
            f"  - 建议动作：{item['suggested_action']}"
        )
    if not issue_lines:
        issue_lines.append("- 当前规则未识别到明确异常；这不代表项目不存在其他风险，仍需业务复核。")
    sources = sorted({str(item.get("data_source")) for item in comparison if item.get("data_source")})
    return "\n".join([
        f"# {project['project_name']}项目周报",
        "",
        "## 1. 项目概况和本周统计周期",
        f"- 商家：{project['merchant_name']}",
        f"- 城市/行业：{project.get('city') or '待补充'} / {project.get('industry') or '待补充'}",
        f"- 门店范围：{project.get('store_scope') or '待补充'}",
        f"- 负责人：{project.get('owner_name') or '待补充'}",
        f"- 统计周期：{current['period_start']} 至 {current['period_end']}",
        "",
        "## 2. 核心指标总览",
        *rows,
        "",
        "## 3. 与上周数据对比",
        "本表按相同指标名称和统计口径计算上周变化；上周为 0 或无可比记录时不计算百分比。",
        "",
        "## 4. 与营销方案目标及当前进度对比",
        _snapshot_summary(project.get("proposal_snapshot") or {}),
        "",
        "## 5. 本周已完成事项",
        _text(current.get("completed_items")),
        "",
        "## 6. 当前异常和可能影响效果的问题",
        f"当前问题：{_text(current.get('current_issues'))}",
        "",
        *issue_lines,
        "",
        "## 7. 需要客户或内部团队确认的事项",
        _text(current.get("pending_confirmations")),
        "",
        "## 8. 下周执行计划",
        _text(current.get("next_week_plan")),
        "",
        "## 9. 数据来源与使用说明",
        f"- 本周数据来源：{'、'.join(sources) if sources else '待补充'}",
        "- 报告数据以平台后台、门店报表和实际执行记录为准。",
        "- 指标变化不自动归因于营销方案，问题分析均为可能原因或待核查项。",
    ])


class OperationsService:
    """协调仓储、确定性计算和报告模板，不自动补造业务事实。"""

    def __init__(self, repository: Any):
        self.repository = repository

    def create_project(self, project: ProjectCreateInput) -> int:
        if not project.metrics:
            raise ValueError("至少定义一项项目跟踪指标。")
        return self.repository.create_project(project)

    def list_projects(self) -> list[dict[str, Any]]:
        return self.repository.list_projects()

    def find_projects(
        self,
        *,
        merchant_name: str,
        project_name: str = "",
        store_scope: str = "",
    ) -> list[dict[str, Any]]:
        """按客户可识别字段查找项目，不要求用户知道数据库主键。"""

        def normalized(value: Any) -> str:
            return str(value or "").strip().casefold()

        merchant = normalized(merchant_name)
        if not merchant:
            return []
        projects = self.list_projects()
        exact = [item for item in projects if normalized(item.get("merchant_name")) == merchant]
        matches = exact or [item for item in projects if merchant in normalized(item.get("merchant_name"))]
        for field, value in (("project_name", project_name), ("store_scope", store_scope)):
            selector = normalized(value)
            if not selector:
                continue
            field_exact = [item for item in matches if normalized(item.get(field)) == selector]
            matches = field_exact or [item for item in matches if selector in normalized(item.get(field))]
        return matches

    def get_project_bundle(self, project_id: int) -> dict[str, Any]:
        project = self.repository.get_project(project_id)
        project["weekly_updates"] = self.repository.list_weekly_updates(project_id)
        project["weekly_reports"] = self.repository.list_weekly_reports(project_id)
        project["closure_report"] = self.repository.get_closure_report(project_id)
        return project

    def save_weekly_update(self, update: WeeklyUpdateInput) -> int:
        project = self.repository.get_project(update.project_id)
        definitions = _definition_map(project)
        unknown = [metric.name for metric in update.metrics if metric.name.casefold() not in definitions]
        if unknown:
            raise ValueError("以下指标尚未在项目中定义：" + "、".join(unknown) + "。")
        return self.repository.save_weekly_update(update)

    def submit_weekly_update(self, update: WeeklyUpdateInput, *, submitted_by: str) -> int:
        """校验销售输入后进入待审核表，不写入正式周度数据。"""

        project = self.repository.get_project(update.project_id)
        definitions = _definition_map(project)
        unknown = [metric.name for metric in update.metrics if metric.name.casefold() not in definitions]
        if unknown:
            raise ValueError("以下指标尚未在项目中定义：" + "、".join(unknown) + "。")
        return self.repository.submit_weekly_update(update, submitted_by=submitted_by)

    def list_pending_weekly(self) -> list[dict[str, Any]]:
        return self.repository.list_pending_weekly()

    def review_weekly_submission(
        self,
        submission_id: int,
        *,
        decision: str,
        reviewer: str,
        note: str = "",
    ) -> dict[str, Any]:
        return self.repository.review_weekly_submission(
            submission_id, decision=decision, reviewer=reviewer, note=note,
        )

    def generate_weekly_report(
        self,
        project_id: int,
        *,
        weekly_update: WeeklyUpdateInput | None = None,
        persist: bool = True,
    ) -> dict[str, Any]:
        project = self.repository.get_project(project_id)
        updates = self.repository.list_weekly_updates(project_id)
        if weekly_update is not None:
            definitions = _definition_map(project)
            unknown = [metric.name for metric in weekly_update.metrics if metric.name.casefold() not in definitions]
            if unknown:
                raise ValueError("以下指标尚未在项目中定义：" + "、".join(unknown) + "。")
            current = {
                "id": None,
                "project_id": weekly_update.project_id,
                "period_start": weekly_update.period_start.isoformat(),
                "period_end": weekly_update.period_end.isoformat(),
                "completed_items": weekly_update.completed_items,
                "current_issues": weekly_update.current_issues,
                "pending_confirmations": weekly_update.pending_confirmations,
                "next_week_plan": weekly_update.next_week_plan,
                "risk_notes": weekly_update.risk_notes,
                "metrics": [
                    {
                        "metric_name": metric.name,
                        "actual_value": str(metric.actual_value),
                        "data_source": metric.source,
                        "note": metric.note,
                    }
                    for metric in weekly_update.metrics
                ],
            }
            updates = [*updates, current]
            # 本次输入尚未审核，不允许将周报或指标写回正式业务表。
            persist = False
        if not updates:
            raise ValueError("该项目还没有已保存的周度数据。")
        comparison = _weekly_comparison(project, updates)
        issues = _weekly_issues(project, updates, comparison)
        report = _weekly_report_markdown(project, updates[-1], comparison, issues)
        report_id = None
        if persist and weekly_update is None:
            report_id = self.repository.save_weekly_report(
                project_id=project_id,
                weekly_update_id=int(updates[-1]["id"]),
                report_markdown=report,
                analysis=issues,
            )
        return {
            "report_id": report_id,
            "project_id": project_id,
            "weekly_update_id": int(updates[-1]["id"]) if updates[-1].get("id") is not None else None,
            "report_markdown": report,
            "comparison": comparison,
            "issues": issues,
        }

    def save_weekly_report_output(
        self,
        generated: Mapping[str, Any],
        *,
        report_markdown: str | None = None,
        analysis: list[dict[str, Any]] | None = None,
    ) -> int:
        """保存专业 Agent 最终发布的周报，不重新计算或修改原始指标。"""

        project_id = generated.get("project_id")
        weekly_update_id = generated.get("weekly_update_id")
        report = report_markdown if report_markdown is not None else generated.get("report_markdown")
        findings = analysis if analysis is not None else generated.get("issues")
        if not isinstance(project_id, int) or project_id <= 0:
            raise ValueError("周报缺少有效项目 ID。")
        if not isinstance(weekly_update_id, int) or weekly_update_id <= 0:
            raise ValueError("周报缺少有效周度记录 ID。")
        if not isinstance(report, str) or not report.strip():
            raise ValueError("周报正文不能为空。")
        if not isinstance(findings, list):
            raise ValueError("周报分析结果格式无效。")
        return self.repository.save_weekly_report(
            project_id=project_id,
            weekly_update_id=weekly_update_id,
            report_markdown=report,
            analysis=findings,
        )

    def generate_closure_report(self, closure: ClosureInput) -> dict[str, Any]:
        project = self.repository.get_project(closure.project_id)
        updates = self.repository.list_weekly_updates(closure.project_id)
        project["weekly_updates"] = updates
        final_map = {metric.name.casefold(): metric for metric in closure.final_metrics}
        comparison: list[dict[str, Any]] = []
        for definition in project.get("metrics", []):
            key = str(definition["metric_name"]).casefold()
            final_metric = final_map.get(key)
            final_value = final_metric.actual_value if final_metric else _aggregate_value(definition, updates)
            source = final_metric.source if final_metric else ("售中周度数据汇总" if final_value is not None else None)
            baseline = _decimal(definition.get("baseline_value"))
            target = _decimal(definition.get("target_value"))
            direction = str(definition.get("target_direction") or "higher")
            completion_ratio, status = _target_result(final_value, target, direction)
            baseline_change, baseline_change_text = (
                _change(final_value, baseline) if final_value is not None else (None, "最终值缺失")
            )
            comparison.append({
                "metric_name": definition["metric_name"],
                "unit": definition.get("unit") or "",
                "baseline_value": str(baseline) if baseline is not None else None,
                "target_value": str(target) if target is not None else None,
                "final_value": str(final_value) if final_value is not None else None,
                "baseline_change_ratio": str(baseline_change) if baseline_change is not None else None,
                "baseline_change_text": baseline_change_text,
                "completion_ratio": str(completion_ratio) if completion_ratio is not None else None,
                "target_status": status,
                "data_source": source,
                "note": final_metric.note if final_metric else "由已保存周度数据按项目聚合方式计算",
            })
        unknown_final = [metric.name for metric in closure.final_metrics if metric.name.casefold() not in _definition_map(project)]
        if unknown_final:
            raise ValueError("以下最终指标尚未在项目中定义：" + "、".join(unknown_final) + "。")
        report = self._closure_markdown(project, updates, closure, comparison)
        return {"report_markdown": report, "comparison": comparison, "project": project}

    def save_closure_report(self, closure: ClosureInput, generated: Mapping[str, Any]) -> int:
        report = generated.get("report_markdown")
        comparison = generated.get("comparison")
        if not isinstance(report, str) or not report.strip() or not isinstance(comparison, list):
            raise ValueError("请先生成合作总结报告草稿。")
        return self.repository.save_closure_report(
            closure,
            report_markdown=report,
            comparison=comparison,
        )

    @staticmethod
    def _closure_markdown(
        project: Mapping[str, Any],
        updates: list[Mapping[str, Any]],
        closure: ClosureInput,
        comparison: list[Mapping[str, Any]],
    ) -> str:
        rows = ["| 指标 | 合作前 | 原定目标 | 最终结果 | 前后变化 | 完成情况 | 数据来源 |", "|---|---:|---:|---:|---|---|---|"]
        for item in comparison:
            unit = str(item.get("unit") or "")
            rows.append(
                f"| {item['metric_name']} | {_display(_decimal(item.get('baseline_value')), unit)} | "
                f"{_display(_decimal(item.get('target_value')), unit)} | {_display(_decimal(item.get('final_value')), unit)} | "
                f"{item['baseline_change_text']} | {item['target_status']} | {item.get('data_source') or '待补充'} |"
            )
        achieved = [item["metric_name"] for item in comparison if item["target_status"] == "已达到"]
        partial = [item["metric_name"] for item in comparison if item["target_status"] == "部分达到"]
        failed = [item["metric_name"] for item in comparison if item["target_status"] == "未达到"]
        unknown = [item["metric_name"] for item in comparison if item["target_status"] == "暂无法判断"]
        completed_from_weeks = [str(item.get("completed_items")) for item in updates if item.get("completed_items")]
        issue_from_weeks = [str(item.get("current_issues")) for item in updates if item.get("current_issues")]
        source_list = sorted({str(item.get("data_source")) for item in comparison if item.get("data_source")})
        return "\n".join([
            f"# {project['project_name']}合作总结报告",
            "",
            "## 1. 合作项目概况",
            f"- 商家：{project['merchant_name']}",
            f"- 项目周期：{project.get('start_date') or '待补充'} 至 {closure.closure_date.isoformat()}",
            f"- 项目最终状态：{CLOSURE_STATUS_LABELS.get(closure.final_status, closure.final_status)}",
            f"- 门店范围：{project.get('store_scope') or '待补充'}",
            f"- 负责人：{project.get('owner_name') or '待补充'}",
            "",
            "## 2. 原定目标与评估口径回顾",
            _snapshot_summary(project.get("proposal_snapshot") or {}),
            "",
            "## 3. 主要执行内容和交付情况",
            _text(closure.completed_items, "\n".join(completed_from_weeks) if completed_from_weeks else "本项尚未补充。"),
            "",
            f"代表性交付物：{_text(closure.deliverables)}",
            "",
            "## 4. 合作前、目标值和最终结果对比",
            *rows,
            "",
            "## 5. 目标完成情况",
            f"- 已达到：{'、'.join(achieved) if achieved else '无'}",
            f"- 部分达到：{'、'.join(partial) if partial else '无'}",
            f"- 未达到：{'、'.join(failed) if failed else '无'}",
            f"- 暂无法判断：{'、'.join(unknown) if unknown else '无'}",
            "",
            "## 6. 合作亮点和有效动作",
            "以下内容仅汇总已录入的完成事项，不表示已完成效果归因。",
            _text(closure.completed_items, "\n".join(completed_from_weeks) if completed_from_weeks else "本项尚未补充。"),
            "",
            "## 7. 存在的问题、遗留事项和数据缺失",
            _text(closure.unresolved_items, "\n".join(issue_from_weeks) if issue_from_weeks else "本项尚未补充。"),
            "",
            "## 8. 经验总结与改进建议",
            "- 对未达到或暂无法判断的指标，优先核对统计口径、数据来源和实际执行范围。",
            "- 指标同期变化不能自动归因于营销方案；需要结合渠道、门店执行和外部环境进一步核查。",
            f"- 限制条件：{_text(closure.limitations)}",
            "",
            "## 9. 后续合作建议",
            f"- 是否建议继续合作：{_text(closure.continuation_recommendation)}",
            f"- 后续方向：{_text(closure.next_direction)}",
            f"- 客户反馈：{_text(closure.customer_feedback)}",
            "",
            "## 10. 数据来源、统计范围和审核提示",
            f"- 最终指标来源：{'、'.join(source_list) if source_list else '待补充'}",
            f"- 已读取售中周度记录：{len(updates)} 期。",
            "- 前后变化、完成率和状态按照统一指标口径计算，不代表确定性效果归因。",
            "- 客户反馈和业务判断应结合原始记录复核后使用。",
        ])
