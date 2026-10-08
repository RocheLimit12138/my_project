"""Gradio 前端：提供售前、售中和售后专业 Agent 入口。"""

from __future__ import annotations

from functools import lru_cache, partial
import json
import logging
import os
from pathlib import Path
import re
import tempfile
from typing import Any

import gradio as gr

from agents.merchant_profile import merchant_profile_from_form
from agents.proposal_agent import ProposalRetrievalError
from agents.sales_script_agent import SalesScriptRetrievalError
from app.case_export import build_case_csv
from app.case_queries import search_cases
from app.llm_config import DeepSeekError
from app.proposal_export import export_proposal_pdf
from app.proposal_pptx_export import export_proposal_pptx
from app.query_context import next_context, rewrite_query
from workflows.case_chat_workflow import CaseChatWorkflow


MODE_CASE = "历史合作案例"
MODE_SALES = "销售话术"
MODE_PROPOSAL = "营销方案"
MODE_TRACKING = "项目跟踪与周报"
MODE_CLOSURE = "合作总结报告"
MODES = (MODE_CASE, MODE_SALES, MODE_PROPOSAL, MODE_TRACKING, MODE_CLOSURE)
MODE_INTENT_HINTS = {
    MODE_CASE: None,
    MODE_SALES: "sales_script",
    MODE_PROPOSAL: "proposal",
    MODE_TRACKING: "project_tracking",
    MODE_CLOSURE: "closure_report",
}

OPERATION_PROMPTS = {
    MODE_TRACKING: (
        "请生成本周项目周报\n"
        "动作：生成周报\n"
        "项目ID：\n"
        "统计开始日期：\n"
        "统计结束日期：\n"
        "本周完成事项：\n"
        "当前问题：\n"
        "客户或内部待确认事项：\n"
        "下周计划：\n"
        "其他风险说明：\n"
        "指标（名称=实际值；来源=数据来源；备注=备注）："
    ),
    MODE_CLOSURE: (
        "请生成合作总结报告\n"
        "动作：生成总结\n"
        "项目ID：\n"
        "合作结束日期：\n"
        "最终状态：已完成\n"
        "主要完成事项：\n"
        "代表性交付物：\n"
        "未完成事项、遗留问题和原因说明：\n"
        "客户反馈或内部评价：\n"
        "是否建议继续合作：\n"
        "后续合作方向：\n"
        "对外报告限制条件：\n"
        "最终指标（名称=实际值；来源=数据来源；备注=备注）："
    ),
}

def _profile_form_fields() -> tuple[str, ...]:
    """表单字段顺序；保持事件输入和转换函数的一致性。"""

    return (
        # 这些是网页端当前保留的最小资料集。MerchantProfile 仍保留旧字段，
        # 以兼容已有 JSON/API 调用，但不再让网页用户填写它们。
        "name", "industry", "city", "address", "seat_count", "business_hours",
        "products", "average_spend_yuan", "problems", "location_notes",
        "target_customers", "goals", "budget_yuan", "campaign_days", "platforms",
    )


def _profile_from_form_values(mode: str, values: tuple[Any, ...]) -> dict[str, Any] | None:
    """只在营销方案模式转换表单；其他模式不携带商家资料。"""

    if mode != MODE_PROPOSAL:
        return None
    fields = _profile_form_fields()
    if len(values) != len(fields):
        raise ValueError("营销方案表单字段数量不匹配，请刷新页面后重试。")
    profile = merchant_profile_from_form(dict(zip(fields, values)))
    return profile.model_dump(mode="json", exclude_none=True)

LOGGER = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_workflow() -> CaseChatWorkflow:
    """每个 Gradio 进程复用一个带 LangGraph 的工作流实例。"""

    return CaseChatWorkflow.from_environment()


def _prepare_question(mode: str, question: str) -> str:
    """用界面模式补充路由上下文，同时保留用户原问题。"""

    prefixes = {
        MODE_CASE: "查询历史合作案例：",
        MODE_SALES: "销售话术问题：",
        MODE_PROPOSAL: "生成营销方案：",
        MODE_TRACKING: "项目跟踪请求：",
        MODE_CLOSURE: "合作总结请求：",
    }
    prefix = prefixes.get(mode)
    if prefix is None:
        raise ValueError("请选择有效的业务能力。")
    cleaned = question.strip()
    if not cleaned:
        raise ValueError("请输入问题。")
    return prefix + cleaned


def _operation_lines(text: str) -> dict[str, str]:
    """读取对话框中的中文键值行，不要求用户填写 JSON。"""

    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or "：" not in line and ":" not in line:
            continue
        key, value = re.split(r"[：:]", line, maxsplit=1)
        key = key.strip(" []【】")
        if key.startswith("指标"):
            key = "指标"
        elif key.startswith("最终指标"):
            key = "最终指标"
        values[key] = value.strip()
    return values


def _operation_int(values: dict[str, str], *keys: str) -> int | None:
    for key in keys:
        raw = values.get(key, "").strip()
        if raw:
            try:
                return int(raw)
            except ValueError as exc:
                raise ValueError(f"{key}必须是整数。") from exc
    return None


def _operation_metrics(raw: str) -> list[dict[str, str]]:
    """解析“名称=值；来源=…”格式的指标，每行一项。"""

    result: list[dict[str, str]] = []
    for line in raw.splitlines():
        line = line.strip().lstrip("-•")
        if not line:
            continue
        fields: dict[str, str] = {}
        for part in re.split(r"[；;]", line):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            fields[key.strip()] = value.strip()
        if not fields:
            continue
        name = fields.get("名称") or fields.get("指标名称")
        actual = fields.get("实际值") or fields.get("数值") or fields.get("值")
        if name and actual:
            result.append({
                "name": name,
                "actual_value": actual,
                "source": fields.get("来源", ""),
                "note": fields.get("备注", ""),
            })
    return result


def _parse_operations_prompt(mode: str, question: str) -> tuple[str, dict[str, Any] | None]:
    """将售中/售后预填文字转换成 Agent 所需的最小结构化动作。"""

    if mode not in (MODE_TRACKING, MODE_CLOSURE):
        return question, None
    values = _operation_lines(question)
    project_id = _operation_int(values, "项目ID", "项目 Id", "项目id")
    action_text = values.get("动作", "")
    if mode == MODE_TRACKING:
        if "保存" in action_text or "保存" in question and "周报" not in question:
            action = "save_weekly"
        elif "加载" in action_text or "查看" in action_text:
            action = "load_project"
        elif "创建" in action_text or "新建" in action_text:
            action = "create_project"
        elif "项目ID" in values and ("周报" in question or "生成" in action_text):
            action = "generate_weekly_report"
        else:
            action = "list_projects"
        payload: dict[str, Any] = {"action": action}
        if project_id is not None:
            payload["project_id"] = project_id
        if action == "save_weekly":
            payload["weekly_update"] = {
                "project_id": project_id,
                "period_start": values.get("统计开始日期", ""),
                "period_end": values.get("统计结束日期", ""),
                "completed_items": values.get("本周完成事项", ""),
                "current_issues": values.get("当前问题", ""),
                "pending_confirmations": values.get("客户或内部待确认事项", ""),
                "next_week_plan": values.get("下周计划", ""),
                "risk_notes": values.get("其他风险说明", ""),
            }
            payload["weekly_metrics"] = _operation_metrics(values.get("指标", ""))
        return "生成项目周报" if action == "generate_weekly_report" else question, payload

    if "保存" in action_text:
        action = "save_report"
    elif "加载" in action_text or "查看" in action_text:
        action = "load_project"
    elif "项目ID" in values or "生成" in action_text or "总结" in question:
        action = "generate_report"
    else:
        action = "list_projects"
    payload = {"action": action}
    if project_id is not None:
        payload["project_id"] = project_id
    if action in {"generate_report", "save_report"}:
        status = values.get("最终状态", "completed")
        status = {
            "已完成": "completed", "部分完成": "partially_completed",
            "已暂停": "paused", "已取消": "cancelled",
        }.get(status, status)
        payload["closure"] = {
            "project_id": project_id,
            "closure_date": values.get("合作结束日期", ""),
            "final_status": status,
            "completed_items": values.get("主要完成事项", ""),
            "deliverables": values.get("代表性交付物", ""),
            "unresolved_items": values.get("未完成事项、遗留问题和原因说明", ""),
            "customer_feedback": values.get("客户反馈或内部评价", ""),
            "continuation_recommendation": values.get("是否建议继续合作", ""),
            "next_direction": values.get("后续合作方向", ""),
            "limitations": values.get("对外报告限制条件", ""),
        }
        payload["final_metrics"] = _operation_metrics(values.get("最终指标", ""))
    return "生成合作总结报告" if action == "generate_report" else question, payload


def _parse_profile(mode: str, profile_text: str | dict[str, Any] | None) -> dict[str, Any] | None:
    """兼容旧 JSON 调用和新表单字典；空白资料不会伪造商家事实。"""

    if isinstance(profile_text, dict):
        if mode != MODE_PROPOSAL:
            raise ValueError("商家资料只适用于营销方案模式。")
        return profile_text
    raw = (profile_text or "").strip()
    if not raw:
        return None
    if mode != MODE_PROPOSAL:
        raise ValueError("商家资料只适用于营销方案模式。")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"商家资料 JSON 格式错误：第 {exc.lineno} 行第 {exc.colno} 列。") from None
    if not isinstance(value, dict):
        raise ValueError("商家资料必须是 JSON 对象。")
    return value


def _warning_markdown(warnings: tuple[str, ...] | list[str]) -> str:
    """将结构化提醒转成稳定的 Markdown 列表。"""

    if not warnings:
        return "无额外提醒。"
    return "\n".join(f"- {item}" for item in warnings)


def _error_outputs(message: str) -> tuple[str, str, list[str], str, None, None]:
    return ("请求失败", message, [], "", None, None)


def _update_profile_input(mode: str) -> gr.Accordion:
    """兼容旧调用：营销方案模式显示结构化商家资料表单。"""

    return gr.Accordion(visible=mode == MODE_PROPOSAL)


def _reset_inputs() -> tuple[Any, ...]:
    """将问题、商家表单和结果控件恢复到页面的初始值。"""

    profile_defaults = ("", "", "", "", None, "", "", None, "", "", "", "", None, None, "")
    return (
        MODE_CASE, "", gr.Accordion(visible=False), *profile_defaults,
        5, None, None, gr.Button(interactive=False), None, None, None, None,
    )


def run_gradio_query(
    mode: str,
    question: str,
    profile_text: str | dict[str, Any] | None,
    top_k: int | float,
    max_distance: float | None,
    workflow: CaseChatWorkflow | None = None,
    operations_payload: dict[str, Any] | None = None,
) -> tuple[str, str, list[str], str, dict[str, Any] | None, dict[str, str | None] | None]:
    """执行一次界面查询，返回状态、答复、来源、提醒和结构化方案证据。"""

    try:
        if isinstance(top_k, bool) or int(top_k) != top_k or not 1 <= int(top_k) <= 20:
            raise ValueError("候选数量必须是 1 到 20 之间的整数。")
        if max_distance is not None and not 0 <= max_distance <= 2:
            raise ValueError("距离阈值必须在 0 到 2 之间。")
        profile = _parse_profile(mode, profile_text)
        result = (workflow or get_workflow()).run(
            _prepare_question(mode, question),
            top_k=int(top_k),
            max_distance=max_distance,
            merchant_profile=profile,
            operations_payload=operations_payload,
            intent_hint=MODE_INTENT_HINTS[mode],
        )
        status = f"{result.status}｜{result.intent}"
        proposal = result.proposal.evidence_dict() if result.proposal is not None else None
        filters = {"city": result.city, "industry": result.industry} if (
            mode == MODE_CASE and result.status == "completed" and result.intent == "case_search"
        ) else None
        if mode == MODE_SALES:
            return status, result.answer, [], "", None, None
        if mode == MODE_PROPOSAL:
            return status, result.answer, [], "", proposal, None
        return status, result.answer, [], _warning_markdown(result.warnings), None, filters
    except DeepSeekError as exc:
        LOGGER.exception(
            "Gradio query failed: mode=%s error_type=%s",
            mode,
            type(exc).__name__,
        )
        return _error_outputs(str(exc))
    except SalesScriptRetrievalError:
        return _error_outputs("销售话术索引暂时不可用，请检查本地模型和索引。")
    except ProposalRetrievalError:
        return _error_outputs("案例文档索引暂时不可用，请确认 PDF/PPTX 已建立索引。")
    except ValueError as exc:
        return _error_outputs(str(exc))
    except Exception as exc:
        LOGGER.error(
            "Gradio query failed: mode=%s error_type=%s",
            mode,
            type(exc).__name__,
        )
        return _error_outputs("前端请求失败，请查看启动 Gradio 的终端日志。")


def export_case_details(filters: dict[str, str | None] | None) -> str:
    """Export only the filters from a completed case search."""

    if filters is None:
        raise gr.Error("请先查询历史合作案例。")
    content = build_case_csv(search_cases, city=filters.get("city"), industry=filters.get("industry"))
    with tempfile.NamedTemporaryFile(prefix="case_details_", suffix=".csv", delete=False) as file:
        file.write(content)
        return file.name


def _query_with_download_state(
    mode: str, question: str, profile_text: str | dict[str, Any] | None, top_k: int | float,
    max_distance: float | None, context: dict[str, Any] | None = None,
    workflow: CaseChatWorkflow | None = None,
    operations_payload: dict[str, Any] | None = None,
) -> tuple[Any, ...]:
    """Clear stale downloads and create both proposal deliverables when complete."""

    try:
        effective_question = rewrite_query(mode, question, context)
    except ValueError as exc:
        return (
            *_error_outputs(str(exc)), gr.Button(visible=False, interactive=False),
            gr.File(visible=False, value=None), gr.File(visible=False, value=None),
            context, gr.File(visible=False, value=None),
        )
    result = run_gradio_query(
        mode, effective_question, profile_text, top_k, max_distance,
        workflow, operations_payload,
    )
    updated_context = next_context(mode, effective_question, result[0], result[5], context)
    proposal_file = None
    proposal_pptx_file = None
    if mode == MODE_PROPOSAL and result[0] == "completed｜proposal":
        try:
            with tempfile.NamedTemporaryFile(prefix="marketing_proposal_", suffix=".pdf", delete=False) as file:
                proposal_file = file.name
            with tempfile.NamedTemporaryFile(prefix="marketing_proposal_", suffix=".pptx", delete=False) as file:
                proposal_pptx_file = file.name
            export_proposal_pdf(result[1], proposal_file)
            export_proposal_pptx(result[4], proposal_pptx_file)
        except Exception as exc:
            LOGGER.error(
                "Proposal export failed: error_type=%s",
                type(exc).__name__,
            )
            for path in (proposal_file, proposal_pptx_file):
                if path is not None:
                    Path(path).unlink(missing_ok=True)
            return (
                *_error_outputs("方案文件生成失败，请查看启动 Gradio 的终端日志。"),
                gr.Button(visible=False, interactive=False),
                gr.File(visible=False, value=None), gr.File(visible=False, value=None),
                None, gr.File(visible=False, value=None),
            )
    return (
        *result,
        gr.Button(
            visible=mode == MODE_CASE and result[5] is not None,
            interactive=result[5] is not None,
        ),
        gr.File(visible=mode == MODE_CASE, value=None),
        gr.File(visible=mode == MODE_PROPOSAL, value=proposal_file),
        updated_context,
        gr.File(visible=mode == MODE_PROPOSAL, value=proposal_pptx_file),
    )


def _query_from_profile_form(
    mode: str,
    question: str,
    top_k: int | float,
    max_distance: float | None,
    *profile_values_and_context: Any,
    workflow: CaseChatWorkflow | None = None,
    operations_payload: dict[str, Any] | None = None,
) -> tuple[Any, ...]:
    """将用户表单转换成 MerchantProfile 后复用既有查询与下载流程。"""

    field_count = len(_profile_form_fields())
    if len(profile_values_and_context) == field_count + 1:
        profile_values = profile_values_and_context[:field_count]
        context = profile_values_and_context[-1]
    else:
        profile_values = profile_values_and_context
        context = None
    try:
        profile = _profile_from_form_values(mode, tuple(profile_values))
    except ValueError as exc:
        return (
            *_error_outputs(str(exc)),
            gr.Button(visible=False, interactive=False),
            gr.File(visible=False, value=None), gr.File(visible=False, value=None),
            context, gr.File(visible=False, value=None),
        )
    return _query_with_download_state(
        mode, question, profile, top_k, max_distance, context, workflow,
        operations_payload,
    )


def _chat_query(
    mode: str,
    question: str,
    top_k: int | float,
    max_distance: float | None,
    *profile_values_and_state: Any,
    workflow: CaseChatWorkflow | None = None,
) -> tuple[Any, ...]:
    """把一次业务查询包装成 ChatGPT 风格的消息流。

    业务查询函数仍返回原有的状态、来源和下载结果；这里只负责把用户问题
    和最终答复追加到中央 Chatbot，并维护左侧的会话标题，避免把展示层逻辑
    混入 Agent 工作流。
    """

    field_count = len(_profile_form_fields())
    if len(profile_values_and_state) != field_count + 4:
        raise ValueError("页面状态字段数量不匹配，请刷新页面后重试。")
    profile_values = profile_values_and_state[:field_count]
    context = profile_values_and_state[field_count]
    chat_history = profile_values_and_state[field_count + 1] or []
    conversation_titles = profile_values_and_state[field_count + 2] or []
    conversation_records = profile_values_and_state[field_count + 3] or []
    cleaned_question = (question or "").strip()
    effective_question, operations_payload = _parse_operations_prompt(mode, cleaned_question)
    result = _query_from_profile_form(
        mode, effective_question, top_k, max_distance, *profile_values, context,
        workflow=workflow, operations_payload=operations_payload,
    )
    status, answer = result[0], result[1]
    updated_history = list(chat_history)
    if cleaned_question:
        updated_history.extend([
            {"role": "user", "content": cleaned_question},
            {"role": "assistant", "content": answer or status},
        ])
    titles = list(conversation_titles)
    if cleaned_question:
        title = " ".join(cleaned_question.split())
        if len(title) > 28:
            title = title[:28].rstrip() + "…"
        if title not in titles:
            titles.append(title)
        records = [item for item in conversation_records if item.get("title") != title]
        records.append({"title": title, "messages": updated_history})
    else:
        records = conversation_records
    selected_title = titles[-1] if titles else None
    return (
        updated_history,
        titles,
        gr.Radio(choices=titles, value=selected_title),
        records,
        "",
        *result,
    )


def _new_chat(records: list[dict[str, Any]] | None = None) -> tuple[Any, ...]:
    """清空当前中央对话，但保留左侧历史会话标题。

    这里只返回普通数据，避免 Gradio 6 在按钮点击时序列化组件更新对象。
    """

    titles = [item.get("title", "") for item in (records or []) if item.get("title")]
    return (
        [], "", [], None, {"choices": titles, "value": None}, "", "", None, "", None,
    )


def _load_conversation(title: str | None, records: list[dict[str, Any]] | None) -> tuple[Any, ...]:
    """从当前浏览器会话的内存记录恢复一段对话。"""

    if not title or not records:
        return [], [], "", None
    for item in records:
        if item.get("title") == title:
            messages = item.get("messages") or []
            return messages, messages, "", None
    return [], [], "", None


def _mode_visibility(
    mode: str,
) -> tuple[gr.Accordion, gr.File, gr.JSON, gr.Markdown, gr.Button, gr.File, None, None, gr.File, gr.Textbox]:
    """Keep each business window focused on its final deliverable."""

    return (
        gr.Accordion(visible=mode == MODE_PROPOSAL),
        gr.File(visible=False, value=None),
        gr.JSON(visible=False, value=None),
        gr.Markdown(visible=True, value=""),
        gr.Button(visible=False, interactive=False),
        gr.File(visible=False, value=None),
        None,
        None,
        gr.File(visible=False, value=None),
        gr.Textbox(value=OPERATION_PROMPTS.get(mode, "")),
    )


def _create_demo_chat(workflow: CaseChatWorkflow | None = None) -> gr.Blocks:
    """单一对话工作台；售中和售后通过对话模板进入，不再单独占页签。"""

    query_fn = _chat_query if workflow is None else partial(_chat_query, workflow=workflow)
    css = """
    :root { --ink: #242529; --muted: #777b83; --line: #e7e4df; --paper: #fffdf9; --accent: #d65f45; }
    .gradio-container { max-width: none !important; padding: 0 !important; background: var(--paper); color: var(--ink); }
    #app-shell { min-height: 860px; gap: 0 !important; }
    #chat-sidebar { flex: 0 0 238px !important; background: #f4f1ec; border-right: 1px solid var(--line); padding: 22px 14px; }
    #chat-sidebar h3 { letter-spacing: -.02em; margin: 0 0 20px; }
    #chat-sidebar .gr-button { border-radius: 10px; border-color: #d9d4cc; background: #fffdf9; color: var(--ink); }
    #history-list { border: 0 !important; background: transparent !important; }
    #history-list label { display: none; }
    #history-list .gr-radio-item { border-radius: 8px; border: 0; padding: 9px 10px; }
    #history-list .gr-radio-item:has(input:checked) { background: #e8e0d8; }
    #main-panel { min-width: 0; padding: 26px 34px 30px; background: var(--paper); }
    #main-header { max-width: 920px; width: 100%; margin: 0 auto 14px; }
    #main-header h1 { font-size: 1.55rem; letter-spacing: -.04em; margin-bottom: 4px; }
    #main-header p { color: var(--muted); margin-top: 0; }
    #chat-window { max-width: 920px; width: 100%; margin: 0 auto; border: 0 !important; background: transparent !important; }
    #chat-window .message { font-size: .98rem; line-height: 1.7; }
    #mode-toolbar { max-width: 920px; width: 100%; margin: 14px auto 8px; }
    #mode-toolbar .wrap { justify-content: center; gap: 5px; }
    #mode-toolbar label, #mode-toolbar input { display: none; }
    #mode-toolbar .gr-radio-item { border: 1px solid #dedad4; border-radius: 999px; padding: 7px 13px; background: #fffdf9; color: #55585e; }
    #mode-toolbar .gr-radio-item:has(input:checked) { background: var(--ink); color: #fff; border-color: var(--ink); }
    #composer { max-width: 920px; width: 100%; margin: 0 auto; border: 1px solid #d5d1ca; border-radius: 17px; padding: 8px; background: #fff; box-shadow: 0 8px 30px rgba(36,37,41,.08); align-items: end; }
    #composer textarea { border: 0 !important; box-shadow: none !important; font-size: 1rem; background: transparent !important; }
    #composer .gr-button { min-width: 72px; border-radius: 11px; background: var(--ink); }
    #proposal-form, #search-settings { max-width: 920px; width: 100%; margin: 0 auto 8px; border-color: var(--line); }
    #status-line, #warning-line { max-width: 920px; width: 100%; margin: 9px auto 0; color: var(--muted); font-size: .88rem; }
    #result-tools { max-width: 920px; width: 100%; margin: 8px auto; }
    @media (max-width: 800px) { #app-shell { display: block !important; } #chat-sidebar { border-right: 0; border-bottom: 1px solid var(--line); } #main-panel { padding: 18px 12px 22px; } #mode-toolbar .gr-radio-item { padding: 6px 8px; font-size: .82rem; } }
    """
    with gr.Blocks(title="点评销售智能助手", css=css) as demo:
        with gr.Row(elem_id="app-shell"):
            with gr.Column(scale=1, min_width=210, elem_id="chat-sidebar"):
                gr.Markdown("### 对话记录")
                new_chat = gr.Button("＋ 新建对话")
                history_list = gr.Radio(choices=[], value=None, label="历史对话", elem_id="history-list")
                gr.Markdown("本记录只保存在当前浏览器会话中。")
            with gr.Column(scale=4, elem_id="main-panel"):
                gr.Markdown("# 点评销售智能助手\n从一个输入框完成案例查询、销售话术、营销方案、项目周报和合作总结。", elem_id="main-header")
                chatbot = gr.Chatbot(label="", show_label=False, height=570, elem_id="chat-window", placeholder="选择上方业务，再从下方输入问题。")
                status = gr.Markdown(value="", elem_id="status-line")
                answer = gr.Markdown(visible=False)
                warnings = gr.Markdown(value="", visible=False, elem_id="warning-line")
                sources = gr.JSON(label="来源", visible=False)
                proposal = gr.JSON(label="营销方案结构化资料与证据", visible=False)
                case_filters = gr.State(None)
                query_context = gr.State(None)
                chat_history = gr.State([])
                conversation_titles = gr.State([])
                conversation_records = gr.State([])
                with gr.Accordion("本次结果附件", open=False, visible=True, elem_id="result-tools"):
                    export_button = gr.Button("导出案例详细数据", visible=False, interactive=False)
                    case_file = gr.File(label="案例详细数据", visible=False)
                    proposal_file = gr.File(label="营销方案 PDF", visible=False)
                    proposal_pptx_file = gr.File(label="营销方案 PPTX", visible=False)
                mode = gr.Radio(choices=list(MODES), value=MODE_CASE, label="", elem_id="mode-toolbar")
                with gr.Accordion("营销方案资料（可选）", open=False, visible=False, elem_id="proposal-form") as proposal_form:
                    gr.Markdown("只填写你已掌握的信息；未填写部分会在方案中标为待补充。")
                    profile_name = gr.Textbox(label="商家名称（必填）")
                    with gr.Row():
                        profile_industry = gr.Textbox(label="品类/赛道（必填）")
                        profile_city = gr.Textbox(label="城市（必填）")
                    profile_address = gr.Textbox(label="具体地址（必填）")
                    with gr.Row():
                        profile_seat_count = gr.Number(label="座位数（可为空）", precision=0)
                        profile_business_hours = gr.Textbox(label="营业时段（可为空）")
                    profile_products = gr.Textbox(label="主营产品（必填）", lines=2)
                    with gr.Row():
                        profile_average_spend = gr.Number(label="客单价/人均消费（元，可为空）", precision=2)
                        profile_location_notes = gr.Textbox(label="当前门店商圈资料（可为空）")
                    profile_problems = gr.Textbox(label="已知问题（必填；每行一项）", lines=3)
                    profile_target_customers = gr.Textbox(label="目标客群（必填）", lines=3)
                    profile_goals = gr.Textbox(label="商家期望目标（必填；每行一项）", lines=3)
                    with gr.Row():
                        profile_budget = gr.Number(label="预算上限（元，必填；可填 0）", precision=2)
                        profile_campaign_days = gr.Number(label="推广周期（天，必填）", precision=0)
                    profile_platforms = gr.Textbox(label="意向平台（必填；每行一项）", lines=2)
                profile_inputs = [profile_name, profile_industry, profile_city, profile_address, profile_seat_count, profile_business_hours, profile_products, profile_average_spend, profile_problems, profile_location_notes, profile_target_customers, profile_goals, profile_budget, profile_campaign_days, profile_platforms]
                with gr.Accordion("检索设置", open=False, elem_id="search-settings"):
                    with gr.Row():
                        top_k = gr.Slider(minimum=1, maximum=20, value=5, step=1, label="候选数量")
                        max_distance = gr.Number(label="距离上限（可选）", minimum=0, maximum=2, value=None, precision=3)
                with gr.Row(elem_id="composer"):
                    question = gr.Textbox(label="", show_label=False, lines=3, placeholder="输入问题，Enter 发送；售中/售后会自动带入文字模板。")
                    submit = gr.Button("发送", variant="primary")
                clear = gr.Button("清空输入", size="sm", variant="secondary")
        query_outputs = [chatbot, conversation_titles, history_list, conversation_records, question, status, answer, sources, warnings, proposal, case_filters, export_button, case_file, proposal_file, query_context, proposal_pptx_file]
        query_inputs = [mode, question, top_k, max_distance, *profile_inputs, query_context, chat_history, conversation_titles, conversation_records]
        submit.click(fn=query_fn, inputs=query_inputs, outputs=query_outputs)
        question.submit(fn=query_fn, inputs=query_inputs, outputs=query_outputs)
        mode.change(fn=_mode_visibility, inputs=mode, outputs=[proposal_form, case_file, proposal, warnings, export_button, proposal_file, case_filters, query_context, proposal_pptx_file, question])
        export_button.click(fn=export_case_details, inputs=case_filters, outputs=case_file)
        new_chat.click(fn=_new_chat, inputs=conversation_records, outputs=[chatbot, question, chat_history, query_context, history_list, status, answer, sources, warnings, proposal])
        history_list.change(fn=_load_conversation, inputs=[history_list, conversation_records], outputs=[chatbot, chat_history, question, query_context])
        clear.click(fn=lambda: ([], "", [], None, "", "", None, "", None, None), outputs=[chatbot, question, chat_history, query_context, status, answer, sources, warnings, proposal, case_filters])
    return demo


def create_demo(workflow: CaseChatWorkflow | None = None) -> gr.Blocks:
    """创建 ChatGPT 风格的售前工作台，并保留售中、售后页签。"""

    return _create_demo_chat(workflow)

    query_fn = _chat_query if workflow is None else partial(_chat_query, workflow=workflow)
    css = """
    .gradio-container { max-width: 1480px !important; padding: 0 !important; }
    #app-shell { min-height: 820px; gap: 0 !important; }
    #chat-sidebar { background: #f7f7f8; border-right: 1px solid #e5e5e5; padding: 18px 14px; }
    #chat-sidebar .gr-button { border-radius: 9px; text-align: left; }
    #history-list { border: 0 !important; background: transparent !important; }
    #history-list label { display: none; }
    #main-panel { min-width: 0; padding: 20px 34px 28px; }
    #main-panel > .prose h1 { margin-top: 0; font-size: 1.45rem; }
    #chat-window { border: 0 !important; background: transparent !important; }
    #chat-window .message { font-size: 0.98rem; line-height: 1.7; }
    #mode-toolbar { max-width: 920px; margin: 12px auto 8px; }
    #mode-toolbar .wrap { justify-content: center; }
    #mode-toolbar label { display: none; }
    #mode-toolbar input { display: none; }
    #mode-toolbar .gr-radio-item { border: 1px solid #d9d9e3; border-radius: 999px; padding: 8px 16px; margin: 0 4px; background: #fff; }
    #mode-toolbar .gr-radio-item:has(input:checked) { background: #343541; color: white; border-color: #343541; }
    #composer { max-width: 920px; margin: 0 auto; border: 1px solid #d9d9e3; border-radius: 16px; padding: 8px; background: white; box-shadow: 0 3px 18px rgba(0,0,0,.06); align-items: end; }
    #composer textarea { border: 0 !important; box-shadow: none !important; font-size: 1rem; }
    #composer .gr-button { min-width: 76px; border-radius: 11px; }
    #status-line, #warning-line { max-width: 920px; margin: 10px auto 0; }
    #proposal-form { max-width: 920px; margin: 0 auto 10px; }
    @media (max-width: 800px) {
      #app-shell { display: block !important; }
      #chat-sidebar { border-right: 0; border-bottom: 1px solid #e5e5e5; }
      #main-panel { padding: 16px 12px 22px; }
    }
    """

    with gr.Blocks(title="点评销售智能助手", css=css) as demo:
        with gr.Row(elem_id="app-shell"):
            with gr.Column(scale=1, min_width=220, elem_id="chat-sidebar"):
                gr.Markdown("### 对话记录")
                new_chat = gr.Button("＋ 新建对话")
                history_list = gr.Radio(
                    choices=[], value=None, label="历史对话", elem_id="history-list",
                )
                gr.Markdown("选择左侧记录可快速确认本次会话主题。")

            with gr.Column(scale=4, elem_id="main-panel"):
                gr.Markdown(
                    "# 点评销售智能助手\n"
                    "用自然语言查询案例、获取销售话术，或生成营销方案。"
                )
                chatbot = gr.Chatbot(
                    label="", show_label=False, height=560,
                    elem_id="chat-window", placeholder="从下方选择功能，然后开始提问。",
                )

                # 以下控件是稳定的结构化结果和下载出口；答复正文由中央 Chatbot 展示。
                status = gr.Markdown(value="", elem_id="status-line")
                answer = gr.Markdown(visible=False)
                warnings = gr.Markdown(value="", label="审核提示", elem_id="warning-line")
                sources = gr.JSON(label="来源", visible=False)
                proposal = gr.JSON(label="营销方案结构化资料与证据", visible=False)
                case_filters = gr.State(None)
                query_context = gr.State(None)
                chat_history = gr.State([])
                conversation_titles = gr.State([])
                export_button = gr.Button("导出案例详细数据", visible=False, interactive=False)
                with gr.Row():
                    case_file = gr.File(label="案例详细数据", visible=False)
                    proposal_file = gr.File(label="营销方案 PDF", visible=False)
                    proposal_pptx_file = gr.File(label="营销方案 PPTX", visible=False)

                mode = gr.Radio(
                    choices=list(MODES), value=MODE_CASE, label="",
                    elem_id="mode-toolbar", interactive=True,
                )
                with gr.Accordion(
                    "营销方案商家资料（可选；仅营销方案模式）",
                    open=False, visible=False, elem_id="proposal-form",
                ) as proposal_form:
                    gr.Markdown(
                        "填写越完整，方案越容易贴合当前商家；未填写的资料会标记为待补充。"
                    )
                    profile_name = gr.Textbox(label="商家名称（必填）")
                    with gr.Row():
                        profile_industry = gr.Textbox(label="品类/赛道（必填）")
                        profile_city = gr.Textbox(label="城市（必填）")
                    profile_address = gr.Textbox(label="具体地址（必填）")
                    with gr.Row():
                        profile_seat_count = gr.Number(label="座位数（可为空）", precision=0)
                        profile_business_hours = gr.Textbox(label="营业时段（可为空）")
                    profile_products = gr.Textbox(label="主营产品（必填）", lines=2)
                    with gr.Row():
                        profile_average_spend = gr.Number(label="客单价/人均消费（元，可为空）", precision=2)
                        profile_location_notes = gr.Textbox(label="当前门店商圈资料（可为空）")
                    profile_problems = gr.Textbox(
                        label="已知问题（必填；每行一项）", lines=3,
                        placeholder="例如：工作日客流不足\n线上曝光有点击但下单少",
                    )
                    profile_target_customers = gr.Textbox(label="目标客群（必填）", lines=3)
                    profile_goals = gr.Textbox(
                        label="商家期望目标（必填；每行一项，不代表效果承诺）",
                        lines=3, placeholder="例如：提升到店订单\n增加优质评价",
                    )
                    with gr.Row():
                        profile_budget = gr.Number(label="预算上限（元，必填；可填 0）", precision=2)
                        profile_campaign_days = gr.Number(label="推广周期（天，必填）", precision=0)
                    profile_platforms = gr.Textbox(
                        label="意向平台（必填；每行一项）", lines=2,
                        placeholder="例如：点评\n微信公众号",
                    )
                profile_inputs = [
                    profile_name, profile_industry, profile_city, profile_address,
                    profile_seat_count, profile_business_hours, profile_products,
                    profile_average_spend, profile_problems, profile_location_notes,
                    profile_target_customers, profile_goals, profile_budget,
                    profile_campaign_days, profile_platforms,
                ]
                with gr.Accordion("检索设置", open=False):
                    with gr.Row():
                        top_k = gr.Slider(
                            minimum=1, maximum=20, value=5, step=1, label="候选数量",
                        )
                        max_distance = gr.Number(
                            label="Cosine 距离上限（可选）", minimum=0, maximum=2,
                            value=None, precision=3,
                        )
                with gr.Row(elem_id="composer"):
                    question = gr.Textbox(
                        label="", show_label=False, lines=2,
                        placeholder="输入问题，按 Enter 发送；Shift+Enter 换行",
                    )
                    submit = gr.Button("发送", variant="primary")
                clear = gr.Button("清空当前输入", size="sm")

        with gr.Tabs():
            with gr.Tab("项目跟踪与周报"):
                build_tracking_tab(proposal)
            with gr.Tab("合作总结报告"):
                build_closure_tab()

        query_outputs = [
            chatbot, conversation_titles, history_list, question,
            status, answer, sources, warnings, proposal, case_filters,
            export_button, case_file, proposal_file, query_context, proposal_pptx_file,
        ]
        query_inputs = [
            mode, question, top_k, max_distance, *profile_inputs,
            query_context, chat_history, conversation_titles,
        ]
        submit.click(fn=query_fn, inputs=query_inputs, outputs=query_outputs)
        question.submit(fn=query_fn, inputs=query_inputs, outputs=query_outputs)
        mode.change(
            fn=_mode_visibility,
            inputs=mode,
            outputs=[
                proposal_form, case_file, proposal, warnings, export_button,
                proposal_file, case_filters, query_context, proposal_pptx_file,
            ],
        )
        export_button.click(fn=export_case_details, inputs=case_filters, outputs=case_file)
        new_chat.click(
            fn=_new_chat,
            outputs=[
                chatbot, question, chat_history, query_context, status, answer,
                sources, warnings, proposal, case_filters, export_button,
                case_file, proposal_file, proposal_pptx_file,
            ],
        )
        clear.click(
            fn=_reset_inputs,
            outputs=[
                mode, question, proposal_form, *profile_inputs, top_k, max_distance,
                case_filters, export_button, case_file, proposal_file, query_context,
                proposal_pptx_file,
            ],
        )
        clear.click(
            fn=lambda: (
                [], "", "", [], "", None, None,
                gr.Button(visible=False, interactive=False),
                gr.File(visible=False, value=None), gr.File(visible=False, value=None),
                None, gr.File(visible=False, value=None),
            ),
            outputs=[
                chatbot, status, answer, sources, warnings, proposal, case_filters,
                export_button, case_file, proposal_file, query_context, proposal_pptx_file,
            ],
        )
    return demo


def launch_gradio() -> None:
    """从环境变量读取监听地址，默认只监听本机。"""

    server_name = os.getenv("GRADIO_SERVER_NAME", "127.0.0.1")
    server_port = int(os.getenv("GRADIO_SERVER_PORT", "7860"))
    create_demo().launch(
        server_name=server_name,
        server_port=server_port,
        theme=gr.themes.Soft(),
    )


if __name__ == "__main__":
    launch_gradio()
