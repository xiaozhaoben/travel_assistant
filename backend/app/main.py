from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .agents import TravelAgentOrchestrator
from .config import get_settings
from .models import ApiResponse, PlanEditRequest, TripPlan, TripPlanRequest
from .services import UnsplashMCPClient

settings = get_settings()

app = FastAPI(title="Travel Assistant API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

orchestrator = TravelAgentOrchestrator()
image_provider = UnsplashMCPClient()


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
        "unsplash_configured": bool(settings.unsplash_access_key),
        "external_api_disabled": settings.disable_external_api,
    }


@app.post("/api/trip/plan", response_model=ApiResponse[TripPlan])
def plan_trip(request: TripPlanRequest):
    plan = orchestrator.plan(request)
    return ApiResponse[TripPlan](success=True, message="行程计划生成成功", data=plan)


@app.post("/api/trip/recalculate", response_model=ApiResponse[TripPlan])
def recalculate_trip(request: PlanEditRequest):
    plan = orchestrator.recalculate(request.plan)
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
