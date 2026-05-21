from __future__ import annotations

from fastapi import FastAPI, HTTPException
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
from .knowledge.news_agent import TravelNewsIngestionAgent, travel_feeds
from .knowledge.qa_agent import TravelQuestionAnsweringAgent
from .knowledge.vector_store import create_travel_vector_store
from .storage.report_store import create_report_store
from .workflows.agents import TravelAgentOrchestrator

settings = get_settings()
setup_logging(settings.log_level)

app = FastAPI(title="Travel Assistant API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

orchestrator = TravelAgentOrchestrator()
report_store = create_report_store(settings.database_url)
travel_vector_store = create_travel_vector_store(settings.database_url)
news_agent = TravelNewsIngestionAgent(travel_vector_store)
qa_agent = TravelQuestionAnsweringAgent(travel_vector_store, llm=orchestrator.planner.llm)
image_provider = (
    UnsplashMCPClient(access_key="", pexels_api_key="", pixabay_api_key="", enable_open_sources=False)
    if settings.disable_external_api
    else UnsplashMCPClient()
)


@app.get("/api/health")
def health():
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
        "database": report_store.health() if report_store is not None else {"enabled": False, "ok": False},
        "travel_knowledge": travel_vector_store.health()
        if travel_vector_store is not None
        else {"enabled": False, "ok": False},
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
    result = orchestrator.plan(request)
    if report_store is not None:
        try:
            report = report_store.save_report(request, result)
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
    result = qa_agent.ask(request.question, top_k=request.top_k)
    return ApiResponse[TravelQAResponse](success=True, message="智能问答完成", data=result)


@app.post("/api/news/ingest", response_model=ApiResponse[TravelNewsIngestResult])
def ingest_travel_news(request: TravelNewsIngestRequest):
    feed_urls = request.feed_urls or travel_feeds
    result = TravelNewsIngestResult.model_validate(news_agent.fetch_travel_feeds(feed_urls))
    if result.errors and result.total_seen == 0:
        raise HTTPException(status_code=503, detail="; ".join(result.errors))
    return ApiResponse[TravelNewsIngestResult](success=True, message="旅行资讯入库完成", data=result)


@app.get("/api/news/status")
def travel_news_status():
    return {
        "success": True,
        "message": "旅行知识库状态",
        "data": {
            "configured_feeds": travel_feeds,
            "knowledge_store": travel_vector_store.health()
            if travel_vector_store is not None
            else {"enabled": False, "ok": False},
        },
    }


@app.post("/api/trip/recalculate", response_model=ApiResponse[TripPlan])
def recalculate_trip(request: PlanEditRequest):
    plan = orchestrator.recalculate(
        request.plan,
        operation=request.operation,
        research_context=request.research_context,
        day_index=request.day_index,
    )
    if request.report_id and report_store is not None:
        try:
            report_store.update_report_plan(
                request.report_id,
                plan,
                operation=request.operation,
                research_context=request.research_context,
            )
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"报告修订写入失败: {exc}") from exc
    return ApiResponse[TripPlan](success=True, message="行程已更新", data=plan)


@app.get("/api/reports", response_model=ApiResponse[list[TripReportSummary]])
def list_reports(limit: int = 50):
    if report_store is None:
        return ApiResponse[list[TripReportSummary]](success=True, message="数据库未启用", data=[])
    try:
        reports = report_store.list_reports(limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"报告数据库查询失败: {exc}") from exc
    return ApiResponse[list[TripReportSummary]](
        success=True,
        message="报告列表获取成功",
        data=reports,
    )


@app.get("/api/reports/{report_id}", response_model=ApiResponse[TripReportDetail])
def get_report(report_id: str):
    if report_store is None:
        raise HTTPException(status_code=503, detail="数据库未启用")
    try:
        report = report_store.get_report(report_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"报告数据库查询失败: {exc}") from exc
    if report is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    return ApiResponse[TripReportDetail](success=True, message="报告详情获取成功", data=report)


@app.get("/api/poi/photo")
def get_poi_photo(name: str):
    return {
        "success": True,
        "message": "获取图片成功",
        "data": {
            "name": name,
            "photo_url": image_provider.image_for(f"{name} China landmark"),
        },
    }


@app.get("/api/map/poi")
def search_map_poi(keywords: str, city: str = "北京", limit: int = 10):
    pois = orchestrator.amap.search_pois(city=city, keywords=[keywords], limit=limit)
    return {
        "success": True,
        "message": "POI搜索成功",
        "data": [poi.model_dump(mode="json") for poi in pois],
    }


@app.get("/api/map/weather")
def get_map_weather(city: str = "北京", days: int = 4):
    from datetime import date

    weather = orchestrator.amap.get_weather(city=city, start=date.today(), days=days)
    return {
        "success": True,
        "message": "天气查询成功",
        "data": [item.model_dump(mode="json") for item in weather],
    }
