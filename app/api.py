"""历史案例查询的 FastAPI 接口层。

本模块只负责 HTTP 参数校验、调用业务函数、序列化响应和映射已知异常。SQL、
数据库连接以及指标聚合继续由查询模块负责，避免接口层与数据访问层耦合。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from app.case_export import build_case_csv

from app.api_models import (
    CaseResponse,
    CaseSearchResponse,
    ErrorResponse,
    HealthResponse,
)
from app.case_queries import MAX_PAGE_SIZE, CaseRecord, get_case_by_id, search_cases
from app.database import DatabaseConfigurationError, DatabaseConnectionError
from app.agent_api import auth_router, router as agent_router


CaseSearcher = Callable[..., list[CaseRecord]]
CaseDetailer = Callable[[int], CaseRecord | None]


def get_case_searcher() -> CaseSearcher:
    """提供案例查询函数，并为接口测试保留可替换的依赖边界。"""

    return search_cases


def get_case_detailer() -> CaseDetailer:
    """供详情接口按需读取单条完整案例，并保留测试依赖替换边界。"""

    return get_case_by_id


app = FastAPI(
    title="点评销售智能助手 API",
    version="0.1.0",
    description="为销售智能助手提供历史合作案例查询能力。",
)
app.include_router(agent_router)
app.include_router(auth_router)

@app.exception_handler(DatabaseConfigurationError)
async def database_configuration_error_handler(
    request: Request,
    exc: DatabaseConfigurationError,
) -> JSONResponse:
    """配置缺失属于服务不可用，不向调用方暴露密码等内部配置。"""

    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": str(exc)},
    )


@app.exception_handler(DatabaseConnectionError)
async def database_connection_error_handler(
    request: Request,
    exc: DatabaseConnectionError,
) -> JSONResponse:
    """将数据库连接故障映射为稳定的 HTTP 503 响应。"""

    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": str(exc)},
    )



@app.get(
    "/cases/export",
    summary="导出历史合作案例详细数据",
)
def export_cases(
    city: Annotated[str | None, Query(max_length=50, description="城市名称")] = None,
    industry: Annotated[str | None, Query(max_length=50, description="商家赛道")] = None,
    case_searcher: Annotated[CaseSearcher, Depends(get_case_searcher)] = search_cases,
) -> StreamingResponse:
    """导出完整案例指标；默认摘要答复不展示的字段在 CSV 中保留。"""

    try:
        content = build_case_csv(case_searcher, city=city, industry=industry)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return StreamingResponse(
        iter([content]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=case_details.csv"},
    )


@app.get(
    "/cases/{case_id}",
    response_model=CaseResponse,
    responses={404: {"model": ErrorResponse, "description": "案例不存在"}},
    summary="按需查看单条历史合作案例详情",
)
def get_case_detail(
    case_id: Annotated[int, Path(ge=1, description="案例内部定位值")],
    case_detailer: Annotated[CaseDetailer, Depends(get_case_detailer)] = get_case_by_id,
) -> CaseResponse:
    """只在用户展开某条案例时加载其完整指标和备注。"""

    try:
        case = case_detailer(case_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="没有找到这条历史合作案例。")
    return CaseResponse.model_validate(case.to_dict())


@app.get(
    "/health",
    response_model=HealthResponse,
    summary="检查 API 进程是否正常",
)
def health_check() -> HealthResponse:
    """返回进程健康状态；该接口不会连接 MySQL。"""

    return HealthResponse(status="ok")


@app.get(
    "/cases",
    response_model=CaseSearchResponse,
    responses={
        400: {"model": ErrorResponse, "description": "查询参数不符合业务规则"},
        503: {"model": ErrorResponse, "description": "数据库服务不可用"},
    },
    summary="查询历史合作案例",
)
def get_cases(
    city: Annotated[
        str | None,
        Query(max_length=50, description="城市名称，例如：武汉"),
    ] = None,
    industry: Annotated[
        str | None,
        Query(max_length=50, description="商家赛道，例如：烧烤"),
    ] = None,
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_PAGE_SIZE, description="本页最多返回的案例数"),
    ] = 20,
    offset: Annotated[
        int,
        Query(ge=0, description="跳过的案例数"),
    ] = 0,
    case_searcher: Annotated[CaseSearcher, Depends(get_case_searcher)] = search_cases,
) -> CaseSearchResponse:
    """校验 HTTP 查询参数并调用统一的案例查询业务函数。"""

    try:
        cases = case_searcher(
            city=city,
            industry=industry,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        # 业务函数仍保留自身校验，便于被 Agent 或脚本直接调用。
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return CaseSearchResponse(
        count=len(cases),
        limit=limit,
        offset=offset,
        cases=[CaseResponse.model_validate(case.to_dict()) for case in cases],
    )
