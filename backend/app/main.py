from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from .core.config import get_settings
from .core.logging_config import setup_logging
from .domain.models import (
    ApiResponse,
    PlanEditRequest,
    TravelNewsIngestRequest,
    TravelNewsIngestResult,
    TravelQARequest,
    TravelQAResponse,
    TripPlan,
    TripPlanningResult,
    TripPlanRequest,
    TripReportDetail,
    TripReportSummary,
)
from .integrations.services import UnsplashMCPClient
from .knowledge.news_agent import TravelNewsIngestionAgent, configured_travel_feeds
from .knowledge.qa_agent import TravelQuestionAnsweringAgent
from .knowledge.vector_store import create_travel_vector_store
from .storage.plan_log import PlanLogRecorder
from .storage.report_store import create_report_store
from .workflows.agents import TravelAgentOrchestrator

settings = get_settings()
setup_logging(settings.log_level)


@dataclass
class AppResources:
    orchestrator: TravelAgentOrchestrator
    report_store: object | None
    travel_vector_store: object | None
    news_agent: TravelNewsIngestionAgent
    qa_agent: TravelQuestionAnsweringAgent
    image_provider: UnsplashMCPClient


orchestrator: TravelAgentOrchestrator | None = None
report_store = None
travel_vector_store = None
news_agent: TravelNewsIngestionAgent | None = None
qa_agent: TravelQuestionAnsweringAgent | None = None
image_provider: UnsplashMCPClient | None = None


def create_app_resources() -> AppResources:
    resource_settings = get_settings()
    resource_orchestrator = TravelAgentOrchestrator()
    resource_report_store = create_report_store(resource_settings.database_url)
    resource_vector_store = create_travel_vector_store(resource_settings.database_url)
    resource_news_agent = TravelNewsIngestionAgent(resource_vector_store)
    resource_qa_agent = TravelQuestionAnsweringAgent(
        resource_vector_store,
        llm=resource_orchestrator.planner.llm,
    )
    resource_image_provider = (
        UnsplashMCPClient(access_key="", pexels_api_key="", pixabay_api_key="", enable_open_sources=False)
        if resource_settings.disable_external_api
        else UnsplashMCPClient()
    )
    return AppResources(
        orchestrator=resource_orchestrator,
        report_store=resource_report_store,
        travel_vector_store=resource_vector_store,
        news_agent=resource_news_agent,
        qa_agent=resource_qa_agent,
        image_provider=resource_image_provider,
    )


def bind_app_resources(resources: AppResources) -> None:
    global orchestrator, report_store, travel_vector_store, news_agent, qa_agent, image_provider
    orchestrator = resources.orchestrator
    report_store = resources.report_store
    travel_vector_store = resources.travel_vector_store
    news_agent = resources.news_agent
    qa_agent = resources.qa_agent
    image_provider = resources.image_provider


def current_global_resources() -> AppResources | None:
    if orchestrator is None:
        return None
    return AppResources(
        orchestrator=orchestrator,
        report_store=report_store,
        travel_vector_store=travel_vector_store,
        news_agent=news_agent or TravelNewsIngestionAgent(travel_vector_store),
        qa_agent=qa_agent or TravelQuestionAnsweringAgent(travel_vector_store, llm=orchestrator.planner.llm),
        image_provider=image_provider or UnsplashMCPClient(),
    )


def get_app_resources() -> AppResources:
    state_resources = getattr(app.state, "resources", None)
    global_resources = current_global_resources()
    if state_resources is not None and (
        (news_agent is not None and news_agent is not state_resources.news_agent)
        or (qa_agent is not None and qa_agent is not state_resources.qa_agent)
        or (image_provider is not None and image_provider is not state_resources.image_provider)
    ):
        resources = AppResources(
            orchestrator=orchestrator or state_resources.orchestrator,
            report_store=report_store if report_store is not None else state_resources.report_store,
            travel_vector_store=travel_vector_store
            if travel_vector_store is not None
            else state_resources.travel_vector_store,
            news_agent=news_agent or state_resources.news_agent,
            qa_agent=qa_agent or state_resources.qa_agent,
            image_provider=image_provider or state_resources.image_provider,
        )
        app.state.resources = resources
        return resources
    if state_resources is not None and global_resources is not None:
        if (
            (orchestrator is not None and state_resources.orchestrator is not global_resources.orchestrator)
            or (report_store is not None and state_resources.report_store is not global_resources.report_store)
            or (
                travel_vector_store is not None
                and state_resources.travel_vector_store is not global_resources.travel_vector_store
            )
            or (news_agent is not None and state_resources.news_agent is not global_resources.news_agent)
            or (qa_agent is not None and state_resources.qa_agent is not global_resources.qa_agent)
            or (image_provider is not None and state_resources.image_provider is not global_resources.image_provider)
        ):
            app.state.resources = global_resources
            return global_resources
    if state_resources is not None:
        return state_resources
    if global_resources is None and any(item is not None for item in (news_agent, qa_agent, image_provider)):
        base_resources = create_app_resources()
        resources = AppResources(
            orchestrator=orchestrator or base_resources.orchestrator,
            report_store=report_store if report_store is not None else base_resources.report_store,
            travel_vector_store=travel_vector_store
            if travel_vector_store is not None
            else base_resources.travel_vector_store,
            news_agent=news_agent or base_resources.news_agent,
            qa_agent=qa_agent or base_resources.qa_agent,
            image_provider=image_provider or base_resources.image_provider,
        )
    else:
        resources = global_resources or create_app_resources()
    bind_app_resources(resources)
    app.state.resources = resources
    return resources


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    resources = current_global_resources() or create_app_resources()
    bind_app_resources(resources)
    fastapi_app.state.resources = resources
    try:
        yield
    finally:
        close_app_resources(resources)
        if getattr(fastapi_app.state, "resources", None) is resources:
            del fastapi_app.state.resources


def close_app_resources(resources: AppResources) -> None:
    for candidate in (
        resources.report_store,
        resources.travel_vector_store,
        resources.image_provider,
        resources.orchestrator.amap,
        resources.orchestrator.unsplash,
    ):
        close = getattr(candidate, "close", None)
        if callable(close):
            close()


app = FastAPI(title="Travel Assistant API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health():
    resources = get_app_resources()
    return {
        "status": "ok",
        "service": "travel-assistant",
        "llm": {
            "enabled": settings.has_llm_credentials,
            "model": settings.llm_model_id,
            "base_url_configured": bool(settings.llm_base_url),
            "disabled": settings.disable_llm,
        },
        "amap_configured": bool(settings.amap_api_key),
        "amap_transport": "mcp-stdio",
        "unsplash_configured": bool(settings.unsplash_access_key),
        "planner_mode": settings.planner_mode,
        "cache_enabled": settings.research_cache_enabled,
        "image_providers": {
            "wikimedia": True,
            "openverse": True,
            "pexels_configured": bool(settings.pexels_api_key),
            "pixabay_configured": bool(settings.pixabay_api_key),
            "unsplash_configured": bool(settings.unsplash_access_key),
        },
        "external_api_disabled": settings.disable_external_api,
        "database": resources.report_store.health()
        if resources.report_store is not None
        else {"enabled": False, "ok": False},
        "travel_knowledge": resources.travel_vector_store.health()
        if resources.travel_vector_store is not None
        else {"enabled": False, "ok": False},
        "web_search": {
            "enabled": bool(settings.web_search_mcp_command),
            "tool": settings.web_search_mcp_tool,
        },
    }


@app.get("/api/trip/plan")
def plan_trip_usage():
    return {
        "success": False,
        "message": "该接口需要使用 POST 提交旅行需求。请回到首页生成行程，或用 POST /api/trip/plan 调用。",
        "method": "POST",
        "example": {
            "prompt": "我想去北京玩 3 天，喜欢历史文化，预算中等",
            "days": 3,
            "pace": "balanced",
            "companions": "friends",
        },
    }


@app.post("/api/trip/plan", response_model=ApiResponse[TripPlanningResult])
def plan_trip(request: TripPlanRequest):
    resources = get_app_resources()
    with PlanLogRecorder() as plan_logs:
        result = resources.orchestrator.plan(request)
    if resources.report_store is not None:
        try:
            report = resources.report_store.save_report(request, result)
            if hasattr(resources.report_store, "save_plan_logs"):
                resources.report_store.save_plan_logs(report["id"], plan_logs.entries)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"报告数据库写入失败: {exc}") from exc
        result = result.model_copy(
            update={
                "report_id": report["id"],
                "report_created_at": report["created_at"],
                "report_updated_at": report["updated_at"],
            }
        )
    return ApiResponse[TripPlanningResult](success=True, message="行程计划生成成功", data=result)


@app.post("/api/qa/ask", response_model=ApiResponse[TravelQAResponse])
def ask_travel_question(request: TravelQARequest):
    resources = get_app_resources()
    result = resources.qa_agent.ask(request.question, top_k=request.top_k)
    return ApiResponse[TravelQAResponse](success=True, message="智能问答完成", data=result)


@app.post("/api/news/ingest", response_model=ApiResponse[TravelNewsIngestResult])
def ingest_travel_news(request: TravelNewsIngestRequest):
    resources = get_app_resources()
    feed_urls = request.feed_urls or configured_travel_feeds()
    result = TravelNewsIngestResult.model_validate(resources.news_agent.fetch_travel_feeds(feed_urls))
    if result.errors and result.total_seen == 0:
        raise HTTPException(status_code=503, detail="; ".join(result.errors))
    return ApiResponse[TravelNewsIngestResult](success=True, message="旅行资讯入库完成", data=result)


@app.get("/api/news/status")
def travel_news_status():
    resources = get_app_resources()
    return {
        "success": True,
        "message": "旅行知识库状态",
        "data": {
            "configured_feeds": configured_travel_feeds(),
            "knowledge_store": resources.travel_vector_store.health()
            if resources.travel_vector_store is not None
            else {"enabled": False, "ok": False},
        },
    }


@app.post("/api/trip/recalculate", response_model=ApiResponse[TripPlan])
def recalculate_trip(request: PlanEditRequest):
    resources = get_app_resources()
    plan = resources.orchestrator.recalculate(
        request.plan,
        operation=request.operation,
        research_context=request.research_context,
        day_index=request.day_index,
    )
    if request.report_id and resources.report_store is not None:
        try:
            resources.report_store.update_report_plan(
                request.report_id,
                plan,
                operation=request.operation,
                research_context=request.research_context,
            )
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"报告修订写入失败: {exc}") from exc
    return ApiResponse[TripPlan](success=True, message="行程已更新", data=plan)


@app.get("/api/reports", response_model=ApiResponse[list[TripReportSummary]])
def list_reports(limit: int = Query(default=50, ge=1, le=200)):
    resources = get_app_resources()
    if resources.report_store is None:
        return ApiResponse[list[TripReportSummary]](success=True, message="数据库未启用", data=[])
    try:
        reports = resources.report_store.list_reports(limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"报告数据库查询失败: {exc}") from exc
    return ApiResponse[list[TripReportSummary]](
        success=True,
        message="报告列表获取成功",
        data=reports,
    )


@app.get("/api/reports/{report_id}", response_model=ApiResponse[TripReportDetail])
def get_report(report_id: str):
    resources = get_app_resources()
    if resources.report_store is None:
        raise HTTPException(status_code=503, detail="数据库未启用")
    try:
        report = resources.report_store.get_report(report_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"报告数据库查询失败: {exc}") from exc
    if report is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    return ApiResponse[TripReportDetail](success=True, message="报告详情获取成功", data=report)


@app.get("/api/poi/photo")
def get_poi_photo(name: str = Query(..., min_length=1, max_length=120)):
    resources = get_app_resources()
    return {
        "success": True,
        "message": "获取图片成功",
        "data": {
            "name": name,
            "photo_url": resources.image_provider.image_for(f"{name} China landmark"),
        },
    }


@app.get("/api/map/poi")
def search_map_poi(
    keywords: str = Query(..., min_length=1, max_length=120),
    city: str = Query(default="北京", min_length=1, max_length=40),
    limit: int = Query(default=10, ge=1, le=30),
):
    resources = get_app_resources()
    pois = resources.orchestrator.amap.search_pois(city=city, keywords=[keywords], limit=limit)
    return {
        "success": True,
        "message": "POI搜索成功",
        "data": [poi.model_dump(mode="json") for poi in pois],
    }


@app.get("/api/map/weather")
def get_map_weather(
    city: str = Query(default="北京", min_length=1, max_length=40),
    days: int = Query(default=4, ge=1, le=10),
):
    from datetime import date

    resources = get_app_resources()
    weather = resources.orchestrator.amap.get_weather(city=city, start=date.today(), days=days)
    return {
        "success": True,
        "message": "天气查询成功",
        "data": [item.model_dump(mode="json") for item in weather],
    }
