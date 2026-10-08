"""售后专业 Agent：复用项目全周期数据并生成受约束的合作总结。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Final, Literal, Mapping

from agents.generation import GeneratedNote, make_note, render_note
from agents.project_resolution import clarification_payload, resolve_project_payload
from app.deepseek_client import JsonModel, create_deepseek_client
from app.operations_models import ClosureInput
from app.operations_repository import MySQLTrackingRepository
from app.operations_service import OperationsService


CLOSURE_REPORT_INTENT: Final = "closure_report"
CLOSURE_REVIEW_WARNING: Final = "合作总结包含记录信息和按统一口径计算的结果，对外使用前需复核，不作为确定性效果归因材料。"
ClosureAction = Literal["list_projects", "load_project", "generate_report", "save_report"]


@dataclass(frozen=True, slots=True)
class ClosureReportRequest:
    """售后 Agent 的动作输入；最终指标与人工判断必须结构化提供。"""

    action: ClosureAction
    question: str = ""
    project_id: int | None = None
    closure: ClosureInput | None = None
    generated: Mapping[str, Any] | None = None

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "ClosureReportRequest":
        question = str(values.get("question") or "").strip()
        project_id = values.get("project_id")
        action = values.get("action")
        if action is None:
            if values.get("closure") is not None or values.get("closure_date") is not None:
                action = "generate_report"
            elif project_id:
                action = "load_project"
            else:
                action = "list_projects"
        if action not in {"list_projects", "load_project", "generate_report", "save_report"}:
            raise ValueError("售后 Agent 动作无效。")
        closure = values.get("closure")
        if closure is not None and not isinstance(closure, ClosureInput):
            if not isinstance(closure, Mapping):
                raise ValueError("closure 必须是结构化结束资料。")
            closure = ClosureInput.from_mapping(closure, metrics=values.get("final_metrics") or ())
        return cls(
            action=action,
            question=question,
            project_id=int(project_id) if project_id is not None else (closure.project_id if closure else None),
            closure=closure,
            generated=values.get("generated"),
        )


@dataclass(frozen=True, slots=True)
class ClosureReportResult:
    """售后 Agent 标准输出。"""

    action: ClosureAction
    answer: str
    payload: dict[str, Any]
    sources: tuple[str, ...]
    warnings: tuple[str, ...]
    found: bool = True
    intent: str = CLOSURE_REPORT_INTENT
    analysis: GeneratedNote | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "intent": self.intent,
            "action": self.action,
            "found": self.found,
            "answer": self.answer,
            "payload": self.payload,
            "sources": list(self.sources),
            "warnings": list(self.warnings),
        }
        if self.analysis is not None:
            result["analysis"] = self.analysis.model_dump(mode="json")
        return result


class ClosureReportAgent:
    """调用售后计算工具；模型只能整理建议，不能修改指标对比。"""

    def __init__(self, service: OperationsService | None = None, *, llm: JsonModel | None = None) -> None:
        self._service = service or OperationsService(MySQLTrackingRepository())
        self._llm = llm

    def run(self, request: ClosureReportRequest | Mapping[str, Any]) -> ClosureReportResult:
        if isinstance(request, Mapping):
            raw_request = dict(request)
            if raw_request.get("action") in {"load_project", "generate_report", "save_report"}:
                resolution = resolve_project_payload(self._service, raw_request, nested_key="closure")
                if resolution.state != "resolved":
                    merchant_name = str(raw_request.get("merchant_name") or "").strip()
                    answer, payload = clarification_payload(resolution, merchant_name=merchant_name)
                    return ClosureReportResult(
                        action=raw_request["action"], answer=answer, payload=payload,
                        sources=("项目跟踪记录",), warnings=(), found=False,
                    )
                raw_request = resolution.payload
            request = ClosureReportRequest.from_mapping(raw_request)
        if not isinstance(request, ClosureReportRequest):
            raise TypeError("request 必须是 ClosureReportRequest 或字典")
        if request.action == "list_projects":
            projects = self._service.list_projects()
            candidates = [item for item in projects if item["project_status"] in {"active", "closing", "completed"}]
            answer = "可用于合作总结的项目：\n" + "\n".join(
                f"- {item['merchant_name']}｜{item['project_name']}｜{item.get('city') or ''} "
                f"{item.get('store_scope') or ''}｜{item['project_status']}"
                for item in candidates
            ) if candidates else "当前没有可用于合作总结的项目。"
            return ClosureReportResult(
                action=request.action, answer=answer, payload={"projects": candidates},
                sources=("项目跟踪记录",), warnings=(CLOSURE_REVIEW_WARNING,), found=bool(candidates),
            )
        project_id = request.project_id or (request.closure.project_id if request.closure is not None else None)
        if project_id is None or project_id <= 0:
            raise ValueError("该动作需要有效的 project_id。")
        if request.action == "load_project":
            bundle = self._service.get_project_bundle(project_id)
            existing = bundle.get("closure_report")
            answer = existing["report_markdown"] if existing else (
                f"已加载“{bundle['project_name']}”的方案快照和 {len(bundle['weekly_updates'])} 期周度记录，"
                "尚未保存合作总结报告。"
            )
            return ClosureReportResult(
                action=request.action, answer=answer, payload={"project": bundle},
                sources=("项目资料与合作记录",),
                warnings=(CLOSURE_REVIEW_WARNING,), found=True,
            )
        if request.closure is None:
            raise ValueError("生成或保存合作总结需要结构化 closure 输入。")
        if request.action == "save_report":
            if request.generated is None:
                raise ValueError("保存前必须提供已生成且未被修改的结构化报告结果。")
            report_id = self._service.save_closure_report(request.closure, request.generated)
            return ClosureReportResult(
                action=request.action,
                answer="合作总结报告已保存。",
                payload={"report_id": report_id, "project_id": request.closure.project_id},
                sources=("合作总结记录",),
                warnings=(CLOSURE_REVIEW_WARNING,),
            )

        generated = self._service.generate_closure_report(request.closure)
        report = generated["report_markdown"]
        result = ClosureReportResult(
            action=request.action, answer=report,
            payload={"generated": generated, "project_id": request.closure.project_id},
            sources=("项目资料", "售中周度记录"),
            warnings=(CLOSURE_REVIEW_WARNING,),
        )
        if self._llm is None:
            return result
        project = generated["project"]
        source_map = {
            "PROJECT": "项目资料和指标定义",
            "PLAN": "原定营销方案",
            "HISTORY": "售中周度记录",
            "FINAL": "最终指标和合作结束信息",
        }
        note = make_note(
            self._llm,
            task="closure_report_agent",
            instructions=(
                "基于代码计算的目标对比和已记录执行信息，整理 1—6 条经验、限制或后续合作建议。"
                "不得修改或新增数字、客户反馈、交付物和原因，不得把同期变化写成确定性归因。"
                "项目定义引用 PROJECT，方案引用 PLAN，过程记录引用 HISTORY，最终输入引用 FINAL。"
            ),
            payload={
                "question": request.question,
                "project": {"id": project["id"], "name": project["project_name"], "status": project["project_status"]},
                "comparison": generated["comparison"],
                "closure_input": {
                    "completed_items": request.closure.completed_items,
                    "unresolved_items": request.closure.unresolved_items,
                    "customer_feedback": request.closure.customer_feedback,
                    "limitations": request.closure.limitations,
                },
                "source_ids": source_map,
            },
            sources=source_map,
        )
        model_section = "### 合作复盘建议\n\n" + render_note(note, source_map)
        marker = "\n## 9. 后续合作建议"
        report = report.replace(marker, "\n\n" + model_section + marker, 1) if marker in report else report + "\n\n" + model_section
        enriched = {**generated, "report_markdown": report, "agent_analysis": note.model_dump(mode="json")}
        return replace(
            result,
            answer=report,
            payload={"generated": enriched, "project_id": request.closure.project_id},
            analysis=note,
        )


def run_closure_report_agent(request: ClosureReportRequest | Mapping[str, Any]) -> ClosureReportResult:
    """函数式入口；常驻服务应复用 Agent 实例。"""

    return ClosureReportAgent(llm=create_deepseek_client()).run(request)
