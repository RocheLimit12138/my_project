"""售中项目跟踪和售后合作总结的独立 Gradio 页签。"""

from __future__ import annotations

from functools import lru_cache
import json
import math
from pathlib import Path
import tempfile
from typing import Any, Mapping

import gradio as gr

from agents.closure_report_agent import ClosureReportAgent, ClosureReportRequest
from agents.project_tracking_agent import ProjectTrackingAgent, ProjectTrackingRequest
from app.deepseek_client import create_deepseek_client
from app.operations_models import ClosureInput, ProjectCreateInput, WeeklyUpdateInput
from app.operations_repository import MySQLTrackingRepository
from app.operations_service import OperationsService
from app.report_export import export_report_pdf


METRIC_DEFINITION_HEADERS = ["指标名称", "单位", "基线值", "目标值", "方向", "聚合方式", "口径说明"]
OBSERVED_METRIC_HEADERS = ["指标名称", "实际值", "数据来源", "备注"]


@lru_cache(maxsize=1)
def get_operations_service() -> OperationsService:
    """每个 Gradio 进程复用无会话状态的 MySQL 仓储服务。"""

    return OperationsService(MySQLTrackingRepository())


@lru_cache(maxsize=1)
def get_operations_agents() -> tuple[ProjectTrackingAgent, ClosureReportAgent]:
    """售中、售后 Agent 共用仓储服务和统一 DeepSeek 客户端。"""

    service = get_operations_service()
    llm = create_deepseek_client()
    return ProjectTrackingAgent(service, llm=llm), ClosureReportAgent(service, llm=llm)


def _tracking_agent(service: OperationsService | None) -> ProjectTrackingAgent:
    return ProjectTrackingAgent(service) if service is not None else get_operations_agents()[0]


def _closure_agent(service: OperationsService | None) -> ClosureReportAgent:
    return ClosureReportAgent(service) if service is not None else get_operations_agents()[1]


def _clean_cell(value: Any) -> Any:
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def _rows(value: Any, headers: list[str]) -> list[list[Any]]:
    if value is None:
        return []
    if hasattr(value, "values") and hasattr(value.values, "tolist"):
        value = value.values.tolist()
    elif hasattr(value, "tolist") and not isinstance(value, list):
        value = value.tolist()
    if not isinstance(value, list):
        raise ValueError("指标表格格式无效。")
    result = []
    for raw in value:
        if isinstance(raw, Mapping):
            row = [_clean_cell(raw.get(header)) for header in headers]
        elif isinstance(raw, (list, tuple)):
            row = [_clean_cell(item) for item in raw]
        else:
            continue
        row += [None] * (len(headers) - len(row))
        row = row[:len(headers)]
        if any(item not in (None, "") for item in row):
            result.append(row)
    return result


def _definition_mappings(value: Any) -> list[dict[str, Any]]:
    return [
        {
            "name": row[0], "unit": row[1], "baseline_value": row[2],
            "target_value": row[3], "direction": row[4] or "越高越好",
            "aggregation": row[5] or "期末", "data_definition": row[6],
        }
        for row in _rows(value, METRIC_DEFINITION_HEADERS)
    ]


def _observed_mappings(value: Any) -> list[dict[str, Any]]:
    return [
        {"name": row[0], "actual_value": row[1], "source": row[2], "note": row[3]}
        for row in _rows(value, OBSERVED_METRIC_HEADERS)
    ]


def _json_object(text: str | None, field_name: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} JSON 格式错误：第 {exc.lineno} 行第 {exc.colno} 列。") from None
    if not isinstance(value, dict):
        raise ValueError(f"{field_name}必须是 JSON 对象。")
    return value


def _project_options_from(projects: list[Mapping[str, Any]]) -> list[tuple[str, int]]:
    return [
        (
            f"{item['project_code']}｜{item['merchant_name']}｜{item['project_name']}｜{item['project_status']}",
            int(item["id"]),
        )
        for item in projects
    ]


def refresh_projects(service: OperationsService | None = None) -> tuple[str, gr.Dropdown]:
    try:
        result = _tracking_agent(service).run(ProjectTrackingRequest(action="list_projects"))
        options = _project_options_from(result.payload["projects"])
        return f"已加载 {len(options)} 个项目。", gr.Dropdown(choices=options, value=None)
    except Exception as exc:
        return f"加载失败：{exc}", gr.Dropdown(choices=[], value=None)


def proposal_to_snapshot(value: dict[str, Any] | None) -> tuple[str, str, str]:
    if not value:
        return "", "manual", "当前会话还没有可复用的营销方案，请先生成方案或手工填写。"
    return json.dumps(value, ensure_ascii=False, indent=2, default=str), "generated_proposal", "已载入当前会话最近一次营销方案结构化快照。"


def create_project(
    project_name: str, merchant_name: str, city: str, industry: str,
    store_scope: str, owner_name: str, start_date: str, end_date: str,
    notes: str, proposal_text: str, proposal_source: str, metric_rows: Any,
    service: OperationsService | None = None,
) -> tuple[str, gr.Dropdown]:
    try:
        project = ProjectCreateInput.from_mapping(
            {
                "project_name": project_name, "merchant_name": merchant_name,
                "city": city, "industry": industry, "store_scope": store_scope,
                "owner_name": owner_name, "start_date": start_date, "end_date": end_date,
                "notes": notes, "proposal_source": proposal_source,
            },
            proposal_snapshot=_json_object(proposal_text, "营销方案快照"),
            metrics=_definition_mappings(metric_rows),
        )
        agent = _tracking_agent(service)
        result = agent.run(ProjectTrackingRequest(action="create_project", project=project))
        project_id = int(result.payload["project_id"])
        projects = agent.run(ProjectTrackingRequest(action="list_projects")).payload["projects"]
        return f"项目已创建，项目 ID：{project_id}。", gr.Dropdown(
            choices=_project_options_from(projects), value=project_id
        )
    except Exception as exc:
        return f"创建失败：{exc}", gr.Dropdown()


def _prefill_observed(project: Mapping[str, Any]) -> list[list[Any]]:
    return [[item["metric_name"], None, None, None] for item in project.get("metrics", [])]


def load_tracking_project(
    project_id: int | None,
    service: OperationsService | None = None,
) -> tuple[str, dict[str, Any] | None, list[list[Any]], dict[str, Any]]:
    try:
        if not project_id:
            raise ValueError("请先选择项目。")
        result = _tracking_agent(service).run(ProjectTrackingRequest(
            action="load_project", project_id=int(project_id),
        ))
        bundle = result.payload["project"]
        history = {
            "weekly_updates": bundle["weekly_updates"],
            "weekly_reports": bundle["weekly_reports"],
        }
        return "项目数据已加载。", bundle, _prefill_observed(bundle), history
    except Exception as exc:
        return f"加载失败：{exc}", None, [], []


def save_weekly_data(
    project_id: int | None, period_start: str, period_end: str, metric_rows: Any,
    completed_items: str, current_issues: str, pending_confirmations: str,
    next_week_plan: str, risk_notes: str,
    service: OperationsService | None = None,
) -> tuple[str, dict[str, Any]]:
    try:
        update = WeeklyUpdateInput.from_mapping(
            {
                "project_id": project_id, "period_start": period_start, "period_end": period_end,
                "completed_items": completed_items, "current_issues": current_issues,
                "pending_confirmations": pending_confirmations, "next_week_plan": next_week_plan,
                "risk_notes": risk_notes,
            },
            metrics=_observed_mappings(metric_rows),
        )
        result = _tracking_agent(service).run(ProjectTrackingRequest(
            action="save_weekly", weekly_update=update,
        ))
        update_id = int(result.payload["weekly_update_id"])
        bundle = result.payload["project"]
        history = {"weekly_updates": bundle["weekly_updates"], "weekly_reports": bundle["weekly_reports"]}
        return f"本周数据已保存，记录 ID：{update_id}。", history
    except Exception as exc:
        return f"保存失败：{exc}", []


def generate_weekly_report(
    project_id: int | None,
    service: OperationsService | None = None,
) -> tuple[str, str, list[dict[str, Any]]]:
    try:
        if not project_id:
            raise ValueError("请先选择项目。")
        result = _tracking_agent(service).run(ProjectTrackingRequest(
            action="generate_weekly_report", project_id=int(project_id),
            question="生成项目周报并分析可能影响效果的问题",
        ))
        return (
            f"周报已由 ProjectTrackingAgent 生成并保存，报告 ID：{result.payload['report_id']}。",
            result.answer,
            result.payload["issues"],
        )
    except Exception as exc:
        return f"生成失败：{exc}", "", []


def load_closure_project(
    project_id: int | None,
    service: OperationsService | None = None,
) -> tuple[str, dict[str, Any] | None, dict[str, Any], list[list[Any]]]:
    try:
        if not project_id:
            raise ValueError("请先选择项目。")
        result = _closure_agent(service).run(ClosureReportRequest(
            action="load_project", project_id=int(project_id),
        ))
        bundle = result.payload["project"]
        history = {"weekly_updates": bundle["weekly_updates"], "weekly_reports": bundle["weekly_reports"]}
        return "项目数据已由 ClosureReportAgent 加载。", bundle, history, _prefill_observed(bundle)
    except Exception as exc:
        return f"加载失败：{exc}", None, {}, []


def generate_closure_report(
    project_id: int | None, closure_date: str, final_status: str, final_metric_rows: Any,
    completed_items: str, deliverables: str, unresolved_items: str,
    customer_feedback: str, continuation_recommendation: str, next_direction: str,
    limitations: str, service: OperationsService | None = None,
) -> tuple[str, str, list[dict[str, Any]], dict[str, Any] | None]:
    try:
        metric_mappings = _observed_mappings(final_metric_rows)
        closure_payload = {
            "project_id": project_id, "closure_date": closure_date, "final_status": final_status,
            "completed_items": completed_items, "deliverables": deliverables,
            "unresolved_items": unresolved_items, "customer_feedback": customer_feedback,
            "continuation_recommendation": continuation_recommendation,
            "next_direction": next_direction, "limitations": limitations,
        }
        closure = ClosureInput.from_mapping(closure_payload, metrics=metric_mappings)
        result = _closure_agent(service).run(ClosureReportRequest(
            action="generate_report", question="生成合作总结报告并整理复盘建议", closure=closure,
        ))
        generated = result.payload["generated"]
        state = {
            "closure_payload": closure_payload,
            "metric_mappings": metric_mappings,
            "generated": {"report_markdown": generated["report_markdown"], "comparison": generated["comparison"]},
        }
        return "合作总结报告草稿已由 ClosureReportAgent 生成，尚未保存。", result.answer, generated["comparison"], state
    except Exception as exc:
        return f"生成失败：{exc}", "", [], None


def save_closure_report(
    state: dict[str, Any] | None,
    service: OperationsService | None = None,
) -> str:
    try:
        if not state:
            raise ValueError("请先生成合作总结报告草稿。")
        closure = ClosureInput.from_mapping(state["closure_payload"], metrics=state["metric_mappings"])
        result = _closure_agent(service).run(ClosureReportRequest(
            action="save_report", closure=closure, generated=state["generated"],
        ))
        return result.answer + " 项目状态已更新。"
    except Exception as exc:
        return f"保存失败：{exc}"


def export_report(markdown: str, report_type: str) -> str:
    if not markdown.strip():
        raise gr.Error("请先生成报告。")
    prefix = "weekly_report_" if report_type == "weekly" else "closure_report_"
    title = "项目周报" if report_type == "weekly" else "合作总结报告"
    with tempfile.NamedTemporaryFile(prefix=prefix, suffix=".pdf", delete=False) as file:
        path = file.name
    try:
        export_report_pdf(markdown, path, title=title)
    except Exception:
        Path(path).unlink(missing_ok=True)
        raise
    return path


def build_tracking_tab(latest_proposal: gr.JSON) -> None:
    """注册“项目跟踪与周报”页签及其持久化事件。"""

    gr.Markdown("## 项目跟踪与周报\n项目、周数据和报告写入 MySQL；所有数据与分析均需业务复核。")
    tracking_status = gr.Textbox(label="操作状态", interactive=False)
    with gr.Row():
        project_selector = gr.Dropdown(label="跟踪项目", choices=[], scale=4)
        refresh_button = gr.Button("刷新项目", scale=1)
        load_button = gr.Button("加载项目", scale=1)
    project_summary = gr.JSON(label="当前项目、方案快照和指标定义")
    weekly_history = gr.JSON(label="已保存周度记录")

    with gr.Accordion("新建跟踪项目", open=False):
        with gr.Row():
            project_name = gr.Textbox(label="项目名称")
            merchant_name = gr.Textbox(label="商家名称")
            owner_name = gr.Textbox(label="负责人")
        with gr.Row():
            city = gr.Textbox(label="城市")
            industry = gr.Textbox(label="行业")
            store_scope = gr.Textbox(label="门店范围")
        with gr.Row():
            start_date = gr.Textbox(label="推广开始日期", placeholder="YYYY-MM-DD")
            end_date = gr.Textbox(label="推广结束日期", placeholder="YYYY-MM-DD")
            proposal_source = gr.Dropdown(
                choices=[("手工录入", "manual"), ("系统生成方案", "generated_proposal")],
                value="manual", label="方案来源",
            )
        notes = gr.Textbox(label="项目备注", lines=2)
        with gr.Row():
            load_proposal_button = gr.Button("载入当前会话最近营销方案")
            create_button = gr.Button("创建跟踪项目", variant="primary")
        proposal_snapshot = gr.Code(label="营销方案结构化快照 JSON", language="json", lines=10)
        metric_definitions = gr.Dataframe(
            headers=METRIC_DEFINITION_HEADERS,
            datatype=["str"] * len(METRIC_DEFINITION_HEADERS),
            type="array",
            label="项目指标定义（方向：越高越好/越低越好/接近目标；聚合：累计/期末）",
        )

    gr.Markdown("### 本周数据与执行信息")
    with gr.Row():
        period_start = gr.Textbox(label="统计开始日期", placeholder="YYYY-MM-DD")
        period_end = gr.Textbox(label="统计结束日期", placeholder="YYYY-MM-DD")
    weekly_metrics = gr.Dataframe(
        headers=OBSERVED_METRIC_HEADERS,
        datatype=["str"] * len(OBSERVED_METRIC_HEADERS),
        type="array", label="本周指标",
    )
    with gr.Row():
        completed_items = gr.Textbox(label="本周完成事项", lines=4)
        current_issues = gr.Textbox(label="当前问题", lines=4)
    with gr.Row():
        pending_confirmations = gr.Textbox(label="客户或内部待确认事项", lines=4)
        next_week_plan = gr.Textbox(label="下周计划", lines=4)
    risk_notes = gr.Textbox(label="其他风险说明", lines=2)
    with gr.Row():
        save_week_button = gr.Button("保存本周数据", variant="primary")
        generate_week_button = gr.Button("生成并保存周报")
        export_week_button = gr.Button("导出周报 PDF")
    weekly_report = gr.Markdown(label="项目周报")
    weekly_issues = gr.JSON(label="可能影响效果的问题与核查建议")
    weekly_file = gr.File(label="周报 PDF")

    refresh_button.click(refresh_projects, outputs=[tracking_status, project_selector])
    load_button.click(
        load_tracking_project, inputs=project_selector,
        outputs=[tracking_status, project_summary, weekly_metrics, weekly_history],
    )
    load_proposal_button.click(
        proposal_to_snapshot, inputs=latest_proposal,
        outputs=[proposal_snapshot, proposal_source, tracking_status],
    )
    create_button.click(
        create_project,
        inputs=[
            project_name, merchant_name, city, industry, store_scope, owner_name,
            start_date, end_date, notes, proposal_snapshot, proposal_source, metric_definitions,
        ],
        outputs=[tracking_status, project_selector],
    )
    save_week_button.click(
        save_weekly_data,
        inputs=[
            project_selector, period_start, period_end, weekly_metrics,
            completed_items, current_issues, pending_confirmations, next_week_plan, risk_notes,
        ],
        outputs=[tracking_status, weekly_history],
    )
    generate_week_button.click(
        generate_weekly_report, inputs=project_selector,
        outputs=[tracking_status, weekly_report, weekly_issues],
    )
    export_week_button.click(
        lambda markdown: export_report(markdown, "weekly"),
        inputs=weekly_report, outputs=weekly_file,
    )


def build_closure_tab() -> None:
    """注册“合作总结报告”页签及其草稿、保存和导出事件。"""

    gr.Markdown("## 合作总结报告\n优先复用方案快照和全部售中周度记录；最终结果不自动归因于营销方案。")
    closure_status = gr.Textbox(label="操作状态", interactive=False)
    with gr.Row():
        project_selector = gr.Dropdown(label="项目", choices=[], scale=4)
        refresh_button = gr.Button("刷新项目", scale=1)
        load_button = gr.Button("加载项目数据", scale=1)
    project_summary = gr.JSON(label="项目与方案摘要")
    weekly_history = gr.JSON(label="完整周度记录")
    with gr.Row():
        closure_date = gr.Textbox(label="合作结束日期", placeholder="YYYY-MM-DD")
        final_status = gr.Dropdown(
            choices=[
                ("已完成", "completed"), ("部分完成", "partially_completed"),
                ("已暂停", "paused"), ("已取消", "cancelled"),
            ], value="completed", label="项目最终状态",
        )
    final_metrics = gr.Dataframe(
        headers=OBSERVED_METRIC_HEADERS,
        datatype=["str"] * len(OBSERVED_METRIC_HEADERS),
        type="array", label="最终指标（留空时可复用周度聚合值）",
    )
    with gr.Row():
        completed_items = gr.Textbox(label="主要完成事项", lines=4)
        deliverables = gr.Textbox(label="代表性交付物", lines=4)
    with gr.Row():
        unresolved_items = gr.Textbox(label="未完成事项、遗留问题和原因说明", lines=4)
        customer_feedback = gr.Textbox(label="客户反馈或内部评价", lines=4)
    with gr.Row():
        continuation_recommendation = gr.Textbox(label="是否建议继续合作", lines=2)
        next_direction = gr.Textbox(label="后续合作方向", lines=2)
    limitations = gr.Textbox(label="对外报告限制条件", lines=2)
    draft_state = gr.State(None)
    with gr.Row():
        generate_button = gr.Button("生成总结报告", variant="primary")
        save_button = gr.Button("保存报告")
        export_button = gr.Button("导出 PDF")
    report = gr.Markdown(label="合作总结报告")
    comparison = gr.JSON(label="代码计算的目标对比")
    report_file = gr.File(label="合作总结报告 PDF")

    refresh_button.click(refresh_projects, outputs=[closure_status, project_selector])
    load_button.click(
        load_closure_project, inputs=project_selector,
        outputs=[closure_status, project_summary, weekly_history, final_metrics],
    )
    generate_button.click(
        generate_closure_report,
        inputs=[
            project_selector, closure_date, final_status, final_metrics,
            completed_items, deliverables, unresolved_items, customer_feedback,
            continuation_recommendation, next_direction, limitations,
        ],
        outputs=[closure_status, report, comparison, draft_state],
    )
    save_button.click(save_closure_report, inputs=draft_state, outputs=closure_status)
    export_button.click(
        lambda markdown: export_report(markdown, "closure"),
        inputs=report, outputs=report_file,
    )
