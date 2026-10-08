"""总控 Agent：识别用户意图、提取查询条件并选择专业 Agent。

支持注入统一 DeepSeek 客户端；未注入时沿用领域规则。模型结果经结构和实体校验，
数据库与工具路由仍由代码控制，输出契约为 ``SupervisorDecision``。
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Any, Final, Literal

from agents.search_agent import SEARCH_INTENT, SearchRequest
from agents.project_tracking_agent import PROJECT_TRACKING_INTENT
from agents.closure_report_agent import CLOSURE_REPORT_INTENT
from app.deepseek_client import JsonModel, create_deepseek_client
from agents.model_routing import classify_with_model


SALES_SCRIPT_INTENT: Final = "sales_script"
PROPOSAL_INTENT: Final = "proposal"
UNKNOWN_INTENT: Final = "unknown"

Intent = Literal["case_search", "sales_script", "proposal", "project_tracking", "closure_report", "unknown"]
IntentHint = Literal["case_search", "sales_script", "proposal", "project_tracking", "closure_report"]
VALID_INTENT_HINTS: Final = frozenset({
    "case_search", "sales_script", "proposal", "project_tracking", "closure_report",
})

SUPPORTED_CITIES: Final = frozenset(
    {
        "北京",
        "上海",
        "广州",
        "深圳",
        "武汉",
        "长沙",
        "杭州",
        "成都",
        "南京",
        "重庆",
        "西安",
        "青岛",
        "苏州",
        "昆明",
        "福州",
        "天津",
        "海口",
        "南宁",
        "兰州",
        "拉萨",
        "呼和浩特",
        "南昌",
        "合肥",
        "济南",
        "无锡",
        "厦门",
    }
)

SUPPORTED_INDUSTRIES: Final = frozenset(
    {
        "烧烤",
        "湖北菜",
        "湘菜",
        "茶饮",
        "火锅",
        "粤菜",
        "本帮菜",
        "淮扬菜",
        "江浙菜",
        "川菜",
        "面食",
        "海鲜",
        "烘焙",
        "云南菜",
        "闽菜",
        "早餐",
        "海南菜",
        "米粉",
        "西藏菜",
        "江西菜",
        "徽菜",
        "鲁菜",
    }
)

PROPOSAL_KEYWORDS: Final = (
    "营销方案",
    "推广方案",
    "策划方案",
    "生成方案",
    "制作方案",
    "提案",
    "ppt",
)

SALES_SCRIPT_KEYWORDS: Final = (
    "销售话术",
    "沟通话术",
    "异议处理",
    "话术",
    "怎么回复客户",
    "怎么回答客户",
    "怎么跟客户说",
)

PROJECT_TRACKING_KEYWORDS: Final = (
    "项目跟踪", "项目周报", "周度跟踪", "周数据", "本周数据",
    "项目进度", "生成周报", "效果问题分析", "投放进度",
)

CLOSURE_REPORT_KEYWORDS: Final = (
    "合作总结报告", "合作总结", "项目总结报告", "结案报告",
    "项目结案", "合作复盘", "项目复盘", "售后总结",
)

CUSTOMER_KEYWORDS: Final = ("客户", "商家", "老板")
REPLY_KEYWORDS: Final = (
    "怎么回复", "如何回复", "怎么回答", "如何回答", "怎么说",
    "怎么沟通", "如何沟通", "怎么应对", "如何应对",
)

CASE_SEARCH_KEYWORDS: Final = (
    "历史合作案例",
    "合作案例",
    "成功案例",
    "历史案例",
    "类似案例",
    "参考案例",
    "合作过",
    "做过",
    "案例",
)

SEARCH_ACTION_KEYWORDS: Final = (
    "查询",
    "查找",
    "查一下",
    "找一下",
    "找找",
    "搜索",
    "看看",
    "有没有",
    "是否有",
)

TARGET_AGENTS: Final = {
    SEARCH_INTENT: "search_agent",
    SALES_SCRIPT_INTENT: "sales_script_agent",
    PROPOSAL_INTENT: "proposal_agent",
    PROJECT_TRACKING_INTENT: "project_tracking_agent",
    CLOSURE_REPORT_INTENT: "closure_report_agent",
}


@dataclass(frozen=True, slots=True)
class SupervisorDecision:
    """总控 Agent 的标准输出，可直接交给后续编排层路由。"""

    question: str
    intent: Intent
    target_agent: str | None
    city: str | None
    industry: str | None
    limit: int
    offset: int
    matched_cities: tuple[str, ...]
    matched_industries: tuple[str, ...]
    needs_clarification: bool
    clarification_questions: tuple[str, ...]
    reason: str

    @property
    def currently_supported(self) -> bool:
        """标记该意图是否已有可执行的专业 Agent。"""

        return self.intent in (
            SEARCH_INTENT, SALES_SCRIPT_INTENT, PROPOSAL_INTENT,
            PROJECT_TRACKING_INTENT, CLOSURE_REPORT_INTENT,
        ) and not self.needs_clarification

    def to_search_request(self) -> SearchRequest:
        """将无歧义的案例查询决策转换为 SearchAgent 输入。"""

        if self.intent != SEARCH_INTENT:
            raise ValueError("只有 case_search 意图可以转换为 SearchRequest")
        if self.needs_clarification:
            raise ValueError("存在未澄清的查询歧义，不能创建 SearchRequest")

        return SearchRequest(
            city=self.city,
            industry=self.industry,
            limit=self.limit,
            offset=self.offset,
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为 API、日志和 LangGraph 状态可使用的普通结构。"""

        return {
            "question": self.question,
            "intent": self.intent,
            "target_agent": self.target_agent,
            "filters": {
                "city": self.city,
                "industry": self.industry,
            },
            "limit": self.limit,
            "offset": self.offset,
            "matched_cities": list(self.matched_cities),
            "matched_industries": list(self.matched_industries),
            "needs_clarification": self.needs_clarification,
            "clarification_questions": list(self.clarification_questions),
            "reason": self.reason,
            "currently_supported": self.currently_supported,
        }


def _normalize_terms(terms: Collection[str], field_name: str) -> tuple[str, ...]:
    """清理可注入词典，避免空词导致所有问题都被误匹配。"""

    normalized: set[str] = set()
    for term in terms:
        if not isinstance(term, str):
            raise TypeError(f"{field_name} 中的值必须是字符串")
        cleaned = term.strip()
        if not cleaned:
            raise ValueError(f"{field_name} 不能包含空字符串")
        normalized.add(cleaned)

    return tuple(sorted(normalized, key=lambda value: (-len(value), value)))


def _find_matches(question: str, terms: tuple[str, ...]) -> tuple[str, ...]:
    """按最长词优先返回问题中出现的领域实体。"""

    return tuple(term for term in terms if term in question)


def _first_keyword(question: str, keywords: tuple[str, ...]) -> str | None:
    """返回首个命中的意图关键词。"""

    lowered = question.lower()
    return next((keyword for keyword in keywords if keyword in lowered), None)


def _classify_intent(
    question: str,
    has_domain_entity: bool,
) -> tuple[Intent, str]:
    """按业务优先级分类；生成方案和话术优先于案例检索。"""

    closure_keyword = _first_keyword(question, CLOSURE_REPORT_KEYWORDS)
    if closure_keyword is not None:
        return CLOSURE_REPORT_INTENT, f"命中合作总结关键词“{closure_keyword}”"

    tracking_keyword = _first_keyword(question, PROJECT_TRACKING_KEYWORDS)
    if tracking_keyword is not None:
        return PROJECT_TRACKING_INTENT, f"命中项目跟踪关键词“{tracking_keyword}”"

    proposal_keyword = _first_keyword(question, PROPOSAL_KEYWORDS)
    if proposal_keyword is not None:
        return PROPOSAL_INTENT, f"命中营销方案关键词“{proposal_keyword}”"

    sales_keyword = _first_keyword(question, SALES_SCRIPT_KEYWORDS)
    if sales_keyword is not None:
        return SALES_SCRIPT_INTENT, f"命中销售话术关键词“{sales_keyword}”"

    if (_first_keyword(question, CUSTOMER_KEYWORDS) is not None
            and _first_keyword(question, REPLY_KEYWORDS) is not None):
        return SALES_SCRIPT_INTENT, "识别到客户沟通对象及回复诉求"

    case_keyword = _first_keyword(question, CASE_SEARCH_KEYWORDS)
    if case_keyword is not None:
        return SEARCH_INTENT, f"命中历史案例关键词“{case_keyword}”"

    action_keyword = _first_keyword(question, SEARCH_ACTION_KEYWORDS)
    if has_domain_entity and action_keyword is not None:
        return SEARCH_INTENT, (
            f"识别到城市或赛道，并命中查询动作“{action_keyword}”"
        )

    return UNKNOWN_INTENT, "未识别到历史案例、销售话术、营销方案、项目跟踪或合作总结相关意图"


class SupervisorAgent:
    """负责意图识别、实体提取和专业 Agent 路由选择。"""

    def __init__(
        self,
        *,
        cities: Collection[str] = SUPPORTED_CITIES,
        industries: Collection[str] = SUPPORTED_INDUSTRIES,
        llm: JsonModel | None = None,
    ) -> None:
        self._llm = llm
        self._cities = _normalize_terms(cities, "cities")
        self._industries = _normalize_terms(industries, "industries")

    def run(
        self,
        question: str,
        *,
        limit: int = 20,
        offset: int = 0,
        intent_hint: IntentHint | None = None,
    ) -> SupervisorDecision:
        """分析一条自然语言问题并返回结构化路由决策。"""

        if not isinstance(question, str):
            raise TypeError("question 必须是字符串")

        normalized_question = question.strip()
        if not normalized_question:
            raise ValueError("question 不能为空")
        if intent_hint is not None and intent_hint not in VALID_INTENT_HINTS:
            raise ValueError("intent_hint 必须是 case_search、sales_script、proposal、project_tracking 或 closure_report")

        matched_cities = _find_matches(normalized_question, self._cities)
        matched_industries = _find_matches(
            normalized_question,
            self._industries,
        )
        if intent_hint is not None:
            intent = intent_hint
            reason = f"调用方已明确指定“{intent_hint}”业务窗口"
        else:
            intent, reason = _classify_intent(
                normalized_question,
                bool(matched_cities or matched_industries),
            )

        model_clarification = ""
        if self._llm is not None and intent_hint is None:
            route = classify_with_model(self._llm, normalized_question)
            intent, reason = route.intent, "DeepSeek 结构化意图识别，查询条件已校验"
            # Normalize an explicit 市 suffix only when it names a known city.
            model_cities = tuple(c[:-1] if c.endswith("市") and c[:-1] in self._cities else c for c in route.cities)
            matched_cities = tuple(dict.fromkeys((*matched_cities, *model_cities)))
            matched_industries = tuple(dict.fromkeys((*matched_industries, *route.industries)))
            model_clarification = route.clarification.strip()

        clarification_questions: list[str] = [model_clarification] if model_clarification else []
        if intent == SEARCH_INTENT and len(matched_cities) > 1:
            clarification_questions.append(
                f"检测到多个城市（{'、'.join(matched_cities)}），请只指定一个城市。"
            )
        if intent == SEARCH_INTENT and len(matched_industries) > 1:
            clarification_questions.append(
                "检测到多个赛道（"
                f"{'、'.join(matched_industries)}），请只指定一个赛道。"
            )
        if intent == UNKNOWN_INTENT:
            clarification_questions.append(
                "请说明你要查询历史案例、查找销售话术、生成营销方案、跟踪项目周报，还是生成合作总结。"
            )

        city = matched_cities[0] if len(matched_cities) == 1 else None
        industry = (
            matched_industries[0]
            if len(matched_industries) == 1
            else None
        )

        return SupervisorDecision(
            question=normalized_question,
            intent=intent,
            target_agent=TARGET_AGENTS.get(intent),
            city=city,
            industry=industry,
            limit=limit,
            offset=offset,
            matched_cities=matched_cities,
            matched_industries=matched_industries,
            needs_clarification=bool(clarification_questions),
            clarification_questions=tuple(clarification_questions),
            reason=reason,
        )


def run_supervisor_agent(
    question: str,
    *,
    limit: int = 20,
    offset: int = 0,
    intent_hint: IntentHint | None = None,
) -> SupervisorDecision:
    """提供函数式入口，供 API 和后续 LangGraph 节点调用。"""

    return SupervisorAgent(llm=create_deepseek_client()).run(
        question,
        limit=limit,
        offset=offset,
        intent_hint=intent_hint,
    )
