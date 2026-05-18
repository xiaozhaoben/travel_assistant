from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .agents import TravelAgentOrchestrator
from .config import get_settings
from .logging_config import setup_logging
from .models import ApiResponse, PlanEditRequest, TripPlan, TripPlanningResult, TripPlanRequest
from .services import UnsplashMCPClient

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
        "image_providers": {
            "wikimedia": True,
            "openverse": True,
            "pexels_configured": bool(settings.pexels_api_key),
            "pixabay_configured": bool(settings.pixabay_api_key),
            "unsplash_configured": bool(settings.unsplash_access_key),
        },
        "external_api_disabled": settings.disable_external_api,
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
    return ApiResponse[TripPlanningResult](success=True, message="行程计划生成成功", data=result)


@app.post("/api/trip/recalculate", response_model=ApiResponse[TripPlan])
def recalculate_trip(request: PlanEditRequest):
    plan = orchestrator.recalculate(
        request.plan,
        operation=request.operation,
        research_context=request.research_context,
        day_index=request.day_index,
    )
    return ApiResponse[TripPlan](success=True, message="行程已更新", data=plan)


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
