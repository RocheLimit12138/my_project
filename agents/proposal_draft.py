"""Render the required template with separate merchant inputs and historical text."""

from __future__ import annotations

import json
from pathlib import Path
from string import Template

from agents.merchant_profile import PROFILE_LABELS
from agents.proposal_agent import ProposalReference, ProposalResult, ProposalSectionResult
from agents.proposal_sections import PROPOSAL_SECTIONS
from agents.generation import clean_customer_copy, render_note, proposal_source_map, quote_text

TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "templates" / "marketing_proposal.md"
PROPOSAL_DRAFT_WARNING = "当前为固定模板草稿；案例候选相关性与适用性尚未验证，预算、排期和效果目标需业务确认。"
NO_REFERENCE_WARNING = "未检索到符合当前条件的历史案例片段，本稿仅提供待完善的规划框架。"
OUTLINE_PREVIEW_WARNING = "仅预览模板，未执行案例检索；历史参考待补充。"


def _quote(value: str) -> str:
    """Keep every user/document line inside a quotation, including headings."""

    return "\n".join(f"> {line}" for line in value.splitlines()) or ">"


def _reference_blocks(references: tuple[ProposalReference, ...]) -> str:
    blocks = []
    for index, reference in enumerate(references, 1):
        review = "已审核" if reference.review_status == "approved" else "待业务审核"
        if reference.source_kind == "simulated_example":
            review = f"模拟案例，{review}"
        blocks.append(
            f"#### 参考 {index}（{review}，候选相关性尚未验证）\n\n"
            f"来源：{_quote(reference.citation).removeprefix('> ')}\n\n"
            f"历史原文：\n\n{_quote(reference.text)}"
        )
    return "\n\n".join(blocks)


def _input_text(value: object) -> str:
    """Display nested metrics without adding missing values or analysis."""

    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _section_block(section: ProposalSectionResult, *, include_references: bool = True) -> str:
    lines = ["### 当前商家资料（用户提供，尚未核验）", ""]
    if section.provided_information:
        for key, value in section.provided_information.items():
            lines.append(_quote(f"{PROFILE_LABELS[key]}：{_input_text(value)}"))
            lines.append("")
    else:
        lines.append("本节暂无已提供的商家资料。")
    if section.missing_fields:
        lines.extend(("", "待补充：" + "、".join(gap.label for gap in section.missing_fields) + "。"))
    else:
        lines.extend(("", "本节所需基础字段已提供，内容仍待核验。"))
    if not include_references:
        for warning in section.warnings:
            if warning not in lines:
                lines.extend(("", f"提示：{warning}"))
        return "\n".join(lines)
    lines.extend(("", "### 本章历史参考", ""))
    if not section.retrieval_performed:
        lines.append(OUTLINE_PREVIEW_WARNING)
    elif section.references:
        lines.append(_reference_blocks(section.references))
    else:
        lines.append("本节未检索到符合当前条件的参考片段。")
    for warning in section.warnings:
        lines.extend(("", f"提示：{warning}"))
    return "\n".join(lines)




def _quoted_record(title: str, fields: tuple[tuple[str, object], ...]) -> str:
    """Render one generated structured record as a readable quoted field list."""

    lines = [f"> **{title}**"]
    for label, value in fields:
        parts = str(value).splitlines() or [""]
        lines.append(f"> - **{label}**：{parts[0]}")
        lines.extend(f">   {part}" for part in parts[1:])
    return "\n".join(lines)


def render_generated_proposal(result: ProposalResult) -> tuple[str, tuple[str, ...]]:
    """Render validated chapters and source material; the model cannot delete evidence."""
    assert result.generated is not None
    sources = proposal_source_map(result)
    by_key = {section.key: section for section in result.sections}
    numerals = "一二三四五六七八"
    merchant_name = result.merchant_profile.name if result.merchant_profile is not None else None
    lines = [f"# {merchant_name or '商家'}营销方案", "",
             "本方案围绕商家现状、目标客群、推广动作与执行计划展开。"
             "具体预算、排期和目标以双方最终确认内容为准。",
             "", "合作目标：", quote_text(clean_customer_copy(result.question))]
    for index, (spec, chapter) in enumerate(zip(PROPOSAL_SECTIONS, result.generated.chapters)):
        lines.extend(("", f"## {numerals[index]}、{spec.title}", "", render_note(chapter, sources)))
        if spec.key == "execution_budget":
            lines.extend(("", "### 建议预算分配（非已确认报价）", ""))
            for index, item in enumerate(result.generated.budget_items, 1):
                lines.append(_quoted_record(
                    f"预算项目 {index}",
                    (("项目", item.item), ("金额", f"{item.amount_yuan:.2f} 元")),
                ))
            if result.generated.budget_items:
                total = sum(item.amount_yuan for item in result.generated.budget_items)
                lines.append(f"\n建议分配合计：{total:.2f} 元。")
            lines.append(quote_text(result.generated.budget_note))
            if result.generated.budget_inquiries:
                lines.extend(("", "### 待询价项目", ""))
                for index, inquiry in enumerate(result.generated.budget_inquiries, 1):
                    lines.append(_quoted_record(
                        f"询价项目 {index}",
                        (
                            ("项目", inquiry.item),
                            ("确认范围", inquiry.scope_to_confirm),
                            ("金额状态", inquiry.quote_status),
                            ("审批/依赖", inquiry.approval_or_dependency),
                        ),
                    ))
            if result.generated.execution_tasks:
                lines.extend(("", "### 当前方案执行任务", ""))
                for index, task in enumerate(result.generated.execution_tasks, 1):
                    lines.append(_quoted_record(
                        f"执行任务 {index}",
                        (
                            ("阶段", task.phase),
                            ("时间", task.relative_days),
                            ("负责人", task.owner),
                            ("任务", task.task),
                            ("交付物", task.deliverables),
                            ("验收", task.acceptance),
                            ("前置条件", task.prerequisites),
                        ),
                    ))
        if spec.key in ("goals", "measurement") and result.generated.measurement_metrics:
            if spec.key == "measurement":
                lines.extend(("", "### 共享指标口径", ""))
                for index, metric in enumerate(result.generated.measurement_metrics, 1):
                    lines.append(_quoted_record(
                        f"指标 {index}",
                        (
                            ("指标", metric.metric),
                            ("定义", metric.definition),
                            ("分子", metric.numerator),
                            ("分母", metric.denominator),
                            ("来源", metric.source),
                            ("周期", metric.period),
                            ("负责人", metric.owner),
                            ("频率", metric.frequency),
                            ("触发条件", metric.adjustment_trigger),
                            ("调整动作", metric.adjustment_action),
                            ("备注", metric.status_notes),
                        ),
                    ))
        if spec.key == "strategy" and result.generated.action_plans:
            lines.extend(("", "### 条件化行动链", ""))
            for index, plan in enumerate(result.generated.action_plans, 1):
                lines.append(_quoted_record(
                    f"行动链 {index}",
                    (
                        ("待验证问题", plan.problem),
                        ("适用客群", plan.audience),
                        ("动作", plan.action),
                        ("前置条件", plan.prerequisites),
                        ("观察指标", plan.metric),
                        ("下一步", plan.next_step),
                    ),
                ))
        lines.extend(("", _section_block(by_key[spec.key], include_references=False)))
    lines.extend(("", "### 证据附录", "", "完整历史原文按来源去重，供审核追溯。", ""))
    seen = set()
    appendix = []
    for reference in result.references:
        key = (reference.source, reference.source_hash, reference.locator, reference.text)
        if key in seen:
            continue
        seen.add(key)
        appendix.append(_reference_blocks((reference,)))
    lines.append("\n\n".join(appendix) if appendix else "本方案未附历史原文。")
    warnings = tuple(dict.fromkeys(result.warnings))
    lines.extend(("", "### 执行前确认", ""))
    lines.extend(quote_text(warning) for warning in warnings)
    return "\n".join(lines).rstrip(), warnings


def render_proposal_draft(result: ProposalResult) -> tuple[str, tuple[str, ...]]:
    """Substitute inputs once; retrieved text never becomes template instructions."""

    if result.generated is not None:
        return render_generated_proposal(result)
    warnings = list(result.warnings)
    warnings.append(PROPOSAL_DRAFT_WARNING)
    if not result.retrieval_performed:
        warnings.append(OUTLINE_PREVIEW_WARNING)
    elif not result.found:
        warnings.append(NO_REFERENCE_WARNING)
    warning_tuple = tuple(dict.fromkeys(warnings))
    by_key = {section.key: section for section in result.sections}
    values = {
        spec.key: _section_block(by_key[spec.key]) if spec.key in by_key
        else "商家资料：待补充。提供结构化商家资料后，本节将展示已知信息、缺失项与定向检索候选。"
        for spec in PROPOSAL_SECTIONS
    }
    if result.sections:
        references = "历史参考已分别列入八个章节；重复引用表示该片段被不同章节检索命中，不代表内容已验证适用。"
    elif result.references:
        references = _reference_blocks(result.references)
    else:
        references = NO_REFERENCE_WARNING if result.retrieval_performed else OUTLINE_PREVIEW_WARNING
    text = Template(TEMPLATE_PATH.read_text(encoding="utf-8")).substitute(
        question=_quote(result.question), references=references,
        warnings="\n".join(f"提示：{warning}" for warning in warning_tuple), **values,
    )
    return text.rstrip(), warning_tuple
