"""自然语言销售助手的 FastAPI 路由。"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.agent_api_models import (
    AgentChatRequest,
    AgentChatResponse,
    LoginRequest,
    OperationsReportExportRequest,
    ProposalExportRequest,
    SwitchAccountRequest,
    WeeklyReviewRequest,
)
from app.auth import (
    SESSION_COOKIE,
    AuthUser,
    authenticate,
    clear_session,
    create_session,
    get_user,
    require_role,
    switch_user,
)
from app.llm_config import DeepSeekError
from app.operations_pptx_export import OperationsPptxExportError, export_operations_report_pptx
from app.proposal_pptx_export import ProposalPptxExportError, export_proposal_pptx
from agents.sales_script_agent import SalesScriptRetrievalError
from agents.proposal_agent import ProposalRetrievalError
from workflows.case_chat_workflow import CaseChatWorkflow


@lru_cache(maxsize=1)
def get_case_chat_workflow() -> CaseChatWorkflow:
    """按进程复用工作流及延迟加载的本地模型，保留测试依赖替换边界。"""

    try:
        return CaseChatWorkflow.from_environment()
    except DeepSeekError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


router = APIRouter(prefix="/agent", tags=["agent"])


auth_router = APIRouter(prefix="/auth", tags=["auth"])


@auth_router.post("/login")
def login(request: LoginRequest, response: Response) -> dict[str, str]:
    user = authenticate(request.username.strip(), request.password)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码不正确。")
    token = create_session(user)
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="lax", secure=False,
        max_age=60 * 60 * 12, path="/",
    )
    return user.to_dict()


@auth_router.get("/me")
def current_user(request: Request) -> dict[str, str]:
    user = get_user(request)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录。")
    return user.to_dict()


@auth_router.post("/switch")
def switch_account(request: SwitchAccountRequest, http_request: Request, response: Response) -> dict[str, str]:
    """免密切换到当前 API 进程中此前成功登录过的账号。"""

    user = switch_user(http_request, request.username)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="该账号尚未在当前服务中登录过，请先输入密码登录一次。",
        )
    token = create_session(user)
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="lax", secure=False,
        max_age=60 * 60 * 12, path="/",
    )
    return user.to_dict()


@auth_router.post("/logout")
def logout(request: Request, response: Response) -> dict[str, str]:
    clear_session(request)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"status": "ok"}


@router.post(
    "/chat",
    response_model=AgentChatResponse,
    responses={
        400: {"description": "请求不符合业务规则"},
        503: {"description": "数据库、检索或 DeepSeek 模型服务不可用"},
    },
    summary="使用自然语言查询销售助手",
)
def chat_with_agent(
    request: AgentChatRequest,
    http_request: Request,
    workflow: Annotated[
        CaseChatWorkflow,
        Depends(get_case_chat_workflow),
    ],
) -> AgentChatResponse:
    """执行自然语言查询工作流并返回统一响应。"""

    operations_payload = dict(request.operations_payload or {})
    action = operations_payload.get("action")
    user = get_user(http_request)
    if action == "save_weekly":
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="保存周数据前请先登录销售账号。")
        if user.role != "sales":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="只有销售用户可以提交周数据。")
        operations_payload["submitted_by"] = user.username
    elif action == "generate_weekly_report" and operations_payload.get("weekly_update") is not None:
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="生成本次周报前请先登录销售账号。")
        if user.role != "sales":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="只有销售用户可以用本次输入生成周报。")
    try:
        result = workflow.run(
            request.question,
            limit=request.limit,
            offset=request.offset,
            top_k=request.top_k,
            max_distance=request.max_distance,
            **({"merchant_profile": request.merchant_profile} if request.merchant_profile is not None else {}),
            **({"operations_payload": operations_payload} if request.operations_payload is not None else {}),
            **({"intent_hint": request.intent_hint} if request.intent_hint is not None else {}),
        )
    except DeepSeekError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except ProposalRetrievalError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="营销方案案例检索暂时不可用，请确认 PDF/PPTX 案例已建立索引，并检查问题长度与本地模型。",
        ) from exc
    except SalesScriptRetrievalError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="销售话术检索暂时不可用，请检查问题长度、本地模型及索引状态后重试。",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return AgentChatResponse.model_validate(result.to_dict())


@router.get("/operations/pending-weekly")
def list_pending_weekly(
    request: Request,
    workflow: Annotated[CaseChatWorkflow, Depends(get_case_chat_workflow)],
) -> dict[str, object]:
    """审核员查看尚未同步正式周度表的销售提交。"""

    require_role(request, "reviewer")
    return {"submissions": workflow.operations_service.list_pending_weekly()}


@router.post("/operations/pending-weekly/{submission_id}/review")
def review_pending_weekly(
    submission_id: int,
    request: WeeklyReviewRequest,
    http_request: Request,
    workflow: Annotated[CaseChatWorkflow, Depends(get_case_chat_workflow)],
) -> dict[str, object]:
    """审核员通过或退回周度提交；通过后才进入正式业务表。"""

    user = require_role(http_request, "reviewer")
    try:
        result = workflow.operations_service.review_weekly_submission(
            submission_id,
            decision=request.decision,
            reviewer=user.username,
            note=request.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return result


@router.post(
    "/proposal/export",
    responses={400: {"description": "方案内容或格式无效"}, 500: {"description": "文件生成失败"}},
    summary="导出已生成的营销方案",
)
def export_proposal(request: ProposalExportRequest) -> Response:
    """根据结构化方案生成可编辑 PPTX。"""

    import tempfile
    from pathlib import Path

    suffix = ".pptx"
    media_type = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    with tempfile.NamedTemporaryFile(prefix="marketing_proposal_", suffix=suffix, delete=False) as file:
        path = Path(file.name)
    try:
        export_proposal_pptx(request.proposal.model_dump(mode="json"), path)
        content = path.read_bytes()
    except (ValueError, ProposalPptxExportError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="方案文件生成失败。") from exc
    finally:
        path.unlink(missing_ok=True)
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="marketing_proposal{suffix}"'},
    )


@router.post(
    "/operations/export",
    responses={400: {"description": "报告内容或格式无效"}, 500: {"description": "文件生成失败"}},
    summary="导出已生成的项目周报或合作总结",
)
def export_operations_report(request: OperationsReportExportRequest) -> Response:
    """把已生成的售中或售后报告排版为可编辑 PPTX，不触及业务数据。"""

    import tempfile
    from pathlib import Path

    suffix = ".pptx"
    media_type = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    filename = "project_weekly_report.pptx" if request.report_type == "weekly_tracking" else "cooperation_summary.pptx"
    with tempfile.NamedTemporaryFile(prefix="operations_report_", suffix=suffix, delete=False) as file:
        path = Path(file.name)
    try:
        export_operations_report_pptx(request.report_type, request.report_markdown, path)
        content = path.read_bytes()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OperationsPptxExportError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="项目报告 PPTX 生成失败。") from exc
    finally:
        path.unlink(missing_ok=True)
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
