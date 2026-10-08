"""售中专业 Agent：编排项目工具、规则分析和受约束的周报建议。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Final, Literal, Mapping

from agents.generation import GeneratedNote, make_note, render_note
from agents.project_resolution import clarification_payload, resolve_project_payload
from app.deepseek_client import JsonModel, create_deepseek_client
from app.operations_models import ProjectCreateInput, WeeklyUpdateInput
from app.operations_repository import MySQLTrackingRepository
from app.operations_service import OperationsService


PROJECT_TRACKING_INTENT: Final = "project_tracking"
TRACKING_REVIEW_WARNING: Final = "项目数据、进度判断和执行建议均需业务复核，不构成效果归因或经营承诺。"
TrackingAction = Literal[
    "list_projects", "load_project", "create_project", "save_weekly", "generate_weekly_report",
    "list_pending_weekly", "review_weekly",
]


@dataclass(frozen=True, slots=True)
class ProjectTrackingRequest:
    """售中 Agent 的显式动作输入；结构化写入不从自然语言中猜测。"""

    action: TrackingAction
    question: str = ""
    project_id: int | None = None
    project: ProjectCreateInput | None = None
    weekly_update: WeeklyUpdateInput | None = None
    submitted_by: str = ""
    submission_id: int | None = None
    review_decision: Literal["approve", "reject"] | None = None
    review_note: str = ""

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "ProjectTrackingRequest":
        question = str(values.get("question") or "").strip()
        project_id = values.get("project_id")
        action = values.get("action")
        if action is None:
            if project_id and any(keyword in question for keyword in ("周报", "效果问题", "风险分析")):
                action = "generate_weekly_report"
            elif project_id:
                action = "load_project"
            else:
                action = "list_projects"
        if action not in {
            "list_projects", "load_project", "create_project", "save_weekly", "generate_weekly_report",
            "list_pending_weekly", "review_weekly",
        }:
            raise ValueError("售中 Agent 动作无效。")

        project = values.get("project")
        if project is not None and not isinstance(project, ProjectCreateInput):
            if not isinstance(project, Mapping):
                raise ValueError("project 必须是结构化项目资料。")
            project = ProjectCreateInput.from_mapping(
                project,
                proposal_snapshot=values.get("proposal_snapshot") or {},
                metrics=values.get("metric_definitions") or (),
            )
        weekly_update = values.get("weekly_update")
        if weekly_update is not None and not isinstance(weekly_update, WeeklyUpdateInput):
            if not isinstance(weekly_update, Mapping):
                raise ValueError("weekly_update 必须是结构化周度资料。")
            weekly_update = WeeklyUpdateInput.from_mapping(
                weekly_update,
                metrics=values.get("weekly_metrics") or (),
            )
        submission_id = values.get("submission_id")
        if submission_id is not None:
            if isinstance(submission_id, bool) or not isinstance(submission_id, int) or submission_id <= 0:
                raise ValueError("submission_id 必须是正整数。")
        review_decision = values.get("review_decision")
        if review_decision is not None and review_decision not in {"approve", "reject"}:
            raise ValueError("review_decision 必须是 approve 或 reject。")
        return cls(
            action=action,
            question=question,
            project_id=int(project_id) if project_id is not None else None,
            project=project,
            weekly_update=weekly_update,
            submitted_by=str(values.get("submitted_by") or "").strip(),
            submission_id=submission_id,
            review_decision=review_decision,
            review_note=str(values.get("review_note") or "").strip(),
        )


@dataclass(frozen=True, slots=True)
class ProjectTrackingResult:
    """售中 Agent 的统一输出，保留工具结果和可展示答复。"""

    action: TrackingAction
    answer: str
    payload: dict[str, Any]
    sources: tuple[str, ...]
    warnings: tuple[str, ...]
    found: bool = True
    intent: str = PROJECT_TRACKING_INTENT
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


class ProjectTrackingAgent:
    """调用确定性售中工具；DeepSeek 只生成待核验建议。"""

    def __init__(self, service: OperationsService | None = None, *, llm: JsonModel | None = None) -> None:
        self._service = service or OperationsService(MySQLTrackingRepository())
        self._llm = llm

    @property
    def service(self) -> OperationsService:
        """供审核路由复用该 Agent 的业务服务。"""

        return self._service

    def run(self, request: ProjectTrackingRequest | Mapping[str, Any]) -> ProjectTrackingResult:
        if isinstance(request, Mapping):
            raw_request = dict(request)
            if raw_request.get("action") in {"load_project", "save_weekly", "generate_weekly_report"}:
                resolution = resolve_project_payload(self._service, raw_request, nested_key="weekly_update")
                if resolution.state != "resolved":
                    merchant_name = str(raw_request.get("merchant_name") or "").strip()
                    answer, payload = clarification_payload(resolution, merchant_name=merchant_name)
                    return ProjectTrackingResult(
                        action=raw_request["action"], answer=answer, payload=payload,
                        sources=("项目跟踪记录",), warnings=(), found=False,
                    )
                raw_request = resolution.payload
            request = ProjectTrackingRequest.from_mapping(raw_request)
        if not isinstance(request, ProjectTrackingRequest):
            raise TypeError("request 必须是 ProjectTrackingRequest 或字典")

        if request.action == "list_projects":
            projects = self._service.list_projects()
            lines = ["当前跟踪项目："] + [
                f"- {item['merchant_name']}｜{item['project_name']}｜{item.get('city') or ''} "
                f"{item.get('store_scope') or ''}｜{item['project_status']}"
                for item in projects
            ] if projects else ["当前还没有已创建的跟踪项目。"]
            return ProjectTrackingResult(
                action=request.action, answer="\n".join(lines), payload={"projects": projects},
                sources=("项目跟踪记录",), warnings=(TRACKING_REVIEW_WARNING,), found=bool(projects),
            )

        if request.action == "create_project":
            if request.project is None:
                raise ValueError("创建项目需要结构化 project 输入。")
            project_id = self._service.create_project(request.project)
            bundle = self._service.get_project_bundle(project_id)
            return ProjectTrackingResult(
                action=request.action,
                answer=f"已创建跟踪项目“{bundle['project_name']}”。",
                payload={"project_id": project_id, "project": bundle},
                sources=("项目资料",), warnings=(TRACKING_REVIEW_WARNING,),
            )

        if request.action == "list_pending_weekly":
            submissions = self._service.list_pending_weekly()
            lines = ["待审核周度提交："] + [
                f"- {item.get('merchant_name') or ''}｜{item.get('project_name') or ''}｜"
                f"{item.get('period_start')} 至 {item.get('period_end')}｜提交人 {item.get('submitted_by') or '未知'}"
                for item in submissions
            ] if submissions else ["当前没有待审核的周度提交。"]
            return ProjectTrackingResult(
                action=request.action, answer="\n".join(lines),
                payload={"submissions": submissions}, sources=("待审核周度提交",),
                warnings=(TRACKING_REVIEW_WARNING,), found=bool(submissions),
            )

        if request.action == "review_weekly":
            if request.submission_id is None:
                raise ValueError("审核周度提交需要 submission_id。")
            if request.review_decision is None:
                raise ValueError("审核周度提交需要 review_decision。")
            reviewed = self._service.review_weekly_submission(
                request.submission_id,
                decision=request.review_decision,
                reviewer=request.submitted_by,
                note=request.review_note,
            )
            answer = "本周数据已审核通过，并已同步到正式周度记录。" if request.review_decision == "approve" else "本周数据已退回，尚未写入正式周度记录。"
            return ProjectTrackingResult(
                action=request.action, answer=answer, payload=reviewed,
                sources=("审核结果",), warnings=(TRACKING_REVIEW_WARNING,),
            )

        if request.action == "save_weekly":
            if request.weekly_update is None:
                raise ValueError("保存周数据需要结构化 weekly_update 输入。")
            submission_id = self._service.submit_weekly_update(
                request.weekly_update,
                submitted_by=request.submitted_by or "sales",
            )
            return ProjectTrackingResult(
                action=request.action,
                answer="本周数据已提交审核，审核员通过后才会同步到正式周度记录。",
                payload={"submission_id": submission_id, "project_id": request.weekly_update.project_id},
                sources=("项目资料", "待审核周度提交"),
                warnings=(TRACKING_REVIEW_WARNING,),
            )

        if request.project_id is None or request.project_id <= 0:
            raise ValueError("该动作需要有效的 project_id。")
        if request.action == "load_project":
            bundle = self._service.get_project_bundle(request.project_id)
            answer = (
                f"已加载“{bundle['project_name']}”：状态 {bundle['project_status']}，"
                f"共 {len(bundle['weekly_updates'])} 期周度记录和 {len(bundle['weekly_reports'])} 份历史周报。"
            )
            return ProjectTrackingResult(
                action=request.action, answer=answer, payload={"project": bundle},
                sources=("项目资料",),
                warnings=(TRACKING_REVIEW_WARNING,),
            )

        generated = self._service.generate_weekly_report(
            request.project_id,
            weekly_update=request.weekly_update,
            persist=False,
        )
        bundle = self._service.get_project_bundle(request.project_id)
        result = ProjectTrackingResult(
            action=request.action,
            answer=generated["report_markdown"],
            payload={"project": bundle, **generated},
            sources=("项目资料", "本周执行与指标记录"),
            warnings=(TRACKING_REVIEW_WARNING,),
        )
        report = generated["report_markdown"]
        if request.weekly_update is not None:
            report += (
                "\n\n> 本次周报使用你刚填写的数据即时生成，尚未写入正式周度记录；"
                "如需进入项目历史，请使用“保存周数据”提交审核。"
            )
        analysis_rows = list(generated["issues"])
        if self._llm is not None:
            source_map = {
                "PROJECT": "项目资料和指标定义",
                "PLAN": "营销方案目标与执行内容",
                "WEEK": "本周执行与指标记录",
            }
            note = make_note(
                self._llm,
                task="project_tracking_agent",
                instructions=(
                    "根据代码计算的指标对比和风险规则，为项目负责人整理 1—5 条核查或执行建议。"
                    "不得重新计算、修改或新增任何数字，不得断言根因，不得承诺效果。"
                    "分析项目定义引用 PROJECT，方案信息引用 PLAN，本周实际与规则结果引用 WEEK。"
                ),
                payload={
                    "question": request.question,
                    "project": {"id": bundle["id"], "name": bundle["project_name"], "status": bundle["project_status"]},
                    "proposal_snapshot": bundle.get("proposal_snapshot") or {},
                    "comparison": generated["comparison"],
                    "rule_issues": generated["issues"],
                    "source_ids": source_map,
                },
                sources=source_map,
            )
            report += "\n\n### 项目跟踪建议\n\n" + render_note(note, source_map)
            analysis_rows.append({"code": "agent_note", "note": note.model_dump(mode="json")})
            result = replace(
                result,
                answer=report,
                payload={**result.payload, "report_markdown": report},
                analysis=note,
            )
        if generated.get("weekly_update_id") is None:
            return replace(result, payload={**result.payload, "report_id": None, "report_persisted": False})
        report_id = self._service.save_weekly_report_output(
            generated,
            report_markdown=report,
            analysis=analysis_rows,
        )
        return replace(result, payload={**result.payload, "report_id": report_id, "report_persisted": True})


def run_project_tracking_agent(request: ProjectTrackingRequest | Mapping[str, Any]) -> ProjectTrackingResult:
    """函数式入口；常驻服务应复用 Agent 实例。"""

    return ProjectTrackingAgent(llm=create_deepseek_client()).run(request)
