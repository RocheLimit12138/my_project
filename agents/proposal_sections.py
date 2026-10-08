"""The eight required proposal chapters and their retrieval preparation rules."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any
from agents.merchant_profile import MerchantProfile, PROFILE_LABELS


@dataclass(frozen=True, slots=True)
class ProfileGap:
    """One missing field, identified by a stable path and a human label."""

    field: str
    label: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SectionSpec:
    """Chapter-specific retrieval focus and relevant current-merchant fields."""

    key: str
    title: str
    keywords: str
    fields: tuple[str, ...]
    required: tuple[str, ...]


PROPOSAL_SECTIONS = (
    SectionSpec("business_profile", "商家基本情况", "品牌品类 主营产品 门店规模 客单价 经营阶段 营业时段 资料截至日期",
                ("name", "industry", "city", "address", "store_count", "store_scope", "business_scale", "seat_count", "business_hours", "business_stage", "products", "average_spend_yuan", "business_status", "data_as_of", "data_source"),
                ("name", "industry", "city", "address", "store_count", "store_scope", "business_scale", "seat_count", "business_hours", "business_stage", "products", "average_spend_yuan", "business_status", "data_as_of")),
    SectionSpec("problem_analysis", "商家现有问题分析", "经营现状 问题 判断依据 原因假设 验证方式",
                ("business_status", "problems", "baseline_metrics", "data_source"),
                ("business_status", "problems", "baseline_metrics", "data_source")),
    SectionSpec("location_analysis", "商家位置与商圈分析", "商圈 周边客群 交通 可达性 竞争门店 消费时段",
                ("city", "address", "location_notes", "data_source"),
                ("city", "address", "location_notes", "data_source")),
    SectionSpec("audience_analysis", "目标客群分析", "核心人群 消费需求 到店动机 适合推广的产品",
                ("target_customers", "products", "average_spend_yuan", "location_notes"),
                ("target_customers", "products")),
    SectionSpec("goals", "推广目标与预期", "目标指标 基线 目标值 衡量方式 统计周期",
                ("goals", "baseline_metrics", "campaign_days", "budget_yuan"),
                ("goals", "baseline_metrics", "campaign_days")),
    SectionSpec("strategy", "营销策略与活动设计", "活动产品 内容主题 平台 渠道 活动机制",
                ("problems", "target_customers", "products", "platforms", "constraints"),
                ("problems", "products", "platforms")),
    SectionSpec("execution_budget", "执行计划与预算", "营销预算 费用项目 金额 分配依据 排期 负责人 交付",
                ("budget_yuan", "campaign_days", "platforms", "store_count", "constraints"),
                ("budget_yuan", "campaign_days", "constraints")),
    SectionSpec("measurement", "效果评估与调整", "数据来源 复盘频率 指标 触发条件 调整动作",
                ("baseline_metrics", "measurement_plan", "platforms", "data_source"),
                ("baseline_metrics", "measurement_plan", "data_source")),
)


def section_query(profile: MerchantProfile, spec: SectionSpec) -> str:
    """Use short city/industry context, leaving full addresses and notes out.

    These are semantic context terms, not SQL/metadata filters. Chapter focus
    must not be drowned out by the full question or a new merchant's name.
    The resulting query is returned to callers for inspection.
    """

    context = " ".join(value for value in (profile.city, profile.industry) if value)
    return f"{context} {spec.title} {spec.keywords}".strip()


def section_inputs(profile: MerchantProfile, spec: SectionSpec) -> tuple[dict[str, Any], tuple[ProfileGap, ...]]:
    """Keep user-provided values separate from reference passages and gaps."""

    data = profile.model_dump(mode="json")
    provided = {key: data[key] for key in spec.fields if data[key] is not None and data[key] != []}
    gaps = [ProfileGap(key, PROFILE_LABELS[key]) for key in spec.required if key not in provided]
    if "baseline_metrics" in provided:
        for index, metric in enumerate(profile.baseline_metrics):
            for key, label in (("period", "统计周期"), ("source", "数据来源")):
                if getattr(metric, key) is None:
                    gaps.append(ProfileGap(f"baseline_metrics.{index}.{key}", f"{metric.name}：{label}"))
    return provided, tuple(gaps)

SECTION_CANDIDATE_MIN = 12
SECTION_TITLE_ALIASES = {
    "business_profile": ("门店分析", "经营概况", "商家基本信息"),
    "problem_analysis": ("问题分析", "门店现状", "门店小红书现状"),
    "location_analysis": ("商圈分析", "选址分析", "地理位置"),
    "audience_analysis": ("客群分析", "目标客群", "用户画像"),
    "goals": ("目标与预期", "推广目标", "营销目标"),
    "strategy": ("营销策略", "推广策略", "小红书笔记营销策略", "活动设计"),
    "execution_budget": ("方案报价", "营销预算", "预算分配", "执行计划", "投放排期"),
    "measurement": ("效果评估", "效果复盘", "复盘与调整"),
}


def has_section_heading(text: str, spec: SectionSpec) -> bool:
    """Recognize explicit chapter headings without treating a contents page as evidence.

    Only the first eight nonempty lines are considered. A very short title-only
    slide or a page listing several required chapter titles gets no promotion.
    This is a transparent ordering heuristic, not verified relevance.
    """

    import re

    if len(text.strip()) < 60:
        return False
    headings = [
        re.sub(r"^[\s#\d一二三四五六七八九十、.．:：()（）-]+", "", line).strip()
        for line in text.splitlines() if line.strip()
    ][:8]
    if sum(section.title in headings for section in PROPOSAL_SECTIONS) >= 3:
        return False
    targets = {spec.title, *SECTION_TITLE_ALIASES[spec.key]}
    return any(line in targets for line in headings)
