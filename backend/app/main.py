from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
import logging

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from .core.config import get_settings
from .core.logging_config import setup_logging
from .domain.models import (
    ApiResponse,
    PlanEditRequest,
    TravelNewsIngestRequest,
    TravelNewsIngestResult,
    TravelQAConversationDetail,
    TravelQAConversationSummary,
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
from .storage.qa_store import create_qa_conversation_store
from .storage.report_store import create_report_store
from .workflows.agents import TravelAgentOrchestrator

settings = get_settings()
setup_logging(settings.log_level)
logger = logging.getLogger(__name__)


@dataclass
class AppResources:
    orchestrator: TravelAgentOrchestrator
    report_store: object | None
    travel_vector_store: object | None
    qa_store: object | None
    news_agent: TravelNewsIngestionAgent
    qa_agent: TravelQuestionAnsweringAgent
    image_provider: UnsplashMCPClient


orchestrator: TravelAgentOrchestrator | None = None
report_store = None
travel_vector_store = None
qa_store = None
news_agent: TravelNewsIngestionAgent | None = None
qa_agent: TravelQuestionAnsweringAgent | None = None
image_provider: UnsplashMCPClient | None = None


def create_app_resources() -> AppResources:
    resource_settings = get_settings()
    resource_report_store = create_report_store(resource_settings.database_url)
    resource_vector_store = create_travel_vector_store(resource_settings.database_url)
    resource_qa_store = create_qa_conversation_store(resource_settings.database_url)
    resource_orchestrator = TravelAgentOrchestrator()
    resource_orchestrator.configure_reflection_memory(resource_vector_store)
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
        qa_store=resource_qa_store,
        news_agent=resource_news_agent,
        qa_agent=resource_qa_agent,
        image_provider=resource_image_provider,
    )


def bind_app_resources(resources: AppResources) -> None:
    global orchestrator, report_store, travel_vector_store, qa_store, news_agent, qa_agent, image_provider
    orchestrator = resources.orchestrator
    report_store = resources.report_store
    travel_vector_store = resources.travel_vector_store
    qa_store = resources.qa_store
    if hasattr(orchestrator, "configure_reflection_memory"):
        orchestrator.configure_reflection_memory(travel_vector_store)
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
        qa_store=qa_store,
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
        or (qa_store is not None and qa_store is not state_resources.qa_store)
        or (image_provider is not None and image_provider is not state_resources.image_provider)
    ):
        resources = AppResources(
            orchestrator=orchestrator or state_resources.orchestrator,
            report_store=report_store if report_store is not None else state_resources.report_store,
            travel_vector_store=travel_vector_store
            if travel_vector_store is not None
            else state_resources.travel_vector_store,
            qa_store=qa_store if qa_store is not None else state_resources.qa_store,
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
            or (qa_store is not None and state_resources.qa_store is not global_resources.qa_store)
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
            qa_store=qa_store if qa_store is not None else base_resources.qa_store,
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
        resources.qa_store,
        resources.image_provider,
        resources.orchestrator.amap,
        resources.orchestrator.unsplash,
    ):
        close = getattr(candidate, "close", None)
        if callable(close):
            close()


def asset_cache_key(asset_type: str, city: str | None, name: str, extra: str | None = None) -> str:
    parts = [asset_type, city or "", name, extra or ""]
    normalized = [" ".join(str(part).strip().lower().split()) for part in parts]
    return "|".join(normalized)


def _valid_cached_url(value: object) -> str:
    text = str(value or "").strip()
    if not text or "source.unsplash.com" in text:
        return ""
    return text


def _call_if_present(target: object, method_name: str, *args, **kwargs):
    method = getattr(target, method_name, None)
    if not callable(method):
        return None
    return method(*args, **kwargs)


def _iter_unique_attractions(result: TripPlanningResult):
    seen: set[tuple[str, str]] = set()
    for option in result.options:
        city = option.plan.city
        for day in option.plan.days:
            for attraction in day.attractions:
                key = (city, attraction.name)
                if key in seen:
                    continue
                seen.add(key)
                yield city, attraction


def _write_report_attraction_image(report_id: str | None, store: object | None, name: str, image_url: str) -> None:
    if not report_id or store is None or not image_url:
        return
    try:
        _call_if_present(store, "update_report_attraction_image", report_id, name, image_url)
    except Exception as exc:  # pragma: no cover - defensive for background enrichment
        logger.warning("Failed to update report attraction image for %s: %s", name, exc)


def _cache_asset(
    store: object | None,
    asset_type: str,
    cache_key: str,
    city: str,
    name: str,
    value: str,
    response_payload: dict | list | None = None,
) -> None:
    if store is None or not value:
        return
    try:
        _call_if_present(
            store,
            "upsert_asset_cache",
            asset_type,
            cache_key,
            city,
            name,
            value,
            response_payload=response_payload,
        )
    except Exception as exc:  # pragma: no cover - defensive for background enrichment
        logger.warning("Failed to cache %s asset for %s: %s", asset_type, name, exc)


def _cached_asset_value(store: object | None, asset_type: str, cache_key: str) -> str:
    if store is None:
        return ""
    try:
        cached = _call_if_present(store, "get_cached_asset", asset_type, cache_key)
    except Exception as exc:
        logger.warning("Failed to read %s asset cache %s: %s", asset_type, cache_key, exc)
        return ""
    if isinstance(cached, dict):
        return _valid_cached_url(cached.get("value"))
    return _valid_cached_url(cached)


def _cached_asset_payload(store: object | None, asset_type: str, cache_key: str):
    if store is None:
        return None
    try:
        cached = _call_if_present(store, "get_cached_asset", asset_type, cache_key)
    except Exception as exc:
        logger.warning("Failed to read %s asset cache %s: %s", asset_type, cache_key, exc)
        return None
    if isinstance(cached, dict):
        return cached.get("response_payload")
    return None


def hydrate_report_assets(report_id: str, result: TripPlanningResult, store: object | None, provider: UnsplashMCPClient) -> None:
    for city, attraction in _iter_unique_attractions(result):
        existing = _valid_cached_url(attraction.image_url)
        key = asset_cache_key("attraction_image", city, attraction.name)
        image_url = existing or _cached_asset_value(store, "attraction_image", key)
        if not image_url:
            try:
                image_url = _valid_cached_url(provider.image_for(f"{city} {attraction.name} China landmark"))
            except Exception as exc:  # pragma: no cover - depends on external providers
                logger.warning("Background image hydration failed for %s: %s", attraction.name, exc)
                image_url = ""
        if not image_url:
            continue
        _cache_asset(
            store,
            "attraction_image",
            key,
            city,
            attraction.name,
            image_url,
            response_payload={"photo_url": image_url},
        )
        _write_report_attraction_image(report_id, store, attraction.name, image_url)


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
    resource_qa_store = getattr(resources, "qa_store", None)
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
        "planner_max_iterations": settings.planner_max_iterations,
        "cache_enabled": settings.research_cache_enabled,
        "image_providers": {
            "web_search": bool(settings.web_search_mcp_command),
            "llm_selector": bool(settings.web_search_mcp_command and settings.has_llm_credentials and not settings.disable_llm),
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
        "qa_memory": resource_qa_store.health()
        if resource_qa_store is not None
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
def plan_trip(request: TripPlanRequest, background_tasks: BackgroundTasks):
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
        background_tasks.add_task(
            hydrate_report_assets,
            report["id"],
            result,
            resources.report_store,
            resources.image_provider,
        )
    return ApiResponse[TripPlanningResult](success=True, message="行程计划生成成功", data=result)


@app.post("/api/qa/ask", response_model=ApiResponse[TravelQAResponse])
def ask_travel_question(request: TravelQARequest):
    resources = get_app_resources()
    resource_qa_store, conversation, conversation_history = _prepare_qa_memory(resources, request)

    result = resources.qa_agent.ask(
        request.question,
        top_k=request.top_k,
        conversation_history=conversation_history,
    )
    result = _persist_qa_exchange(resource_qa_store, conversation, request.question, result)
    return ApiResponse[TravelQAResponse](success=True, message="智能问答完成", data=result)


@app.post("/api/qa/ask/stream")
def stream_travel_question(request: TravelQARequest):
    resources = get_app_resources()
    resource_qa_store, conversation, conversation_history = _prepare_qa_memory(resources, request)

    def event_stream():
        answer_parts: list[str] = []
        final_response: TravelQAResponse | None = None
        try:
            yield _sse_event(
                "start",
                {
                    "conversation_id": conversation["id"] if conversation else None,
                    "question": request.question,
                },
            )
            stream = getattr(resources.qa_agent, "stream", None)
            if callable(stream):
                events = stream(request.question, top_k=request.top_k, conversation_history=conversation_history)
            else:
                events = _response_to_stream_events(resources.qa_agent.ask(request.question, top_k=request.top_k))

            for event in events:
                event_name = str(event.get("event") or "message")
                data = event.get("data")
                if event_name == "answer_delta":
                    content = str((data or {}).get("content") if isinstance(data, dict) else data or "")
                    if content:
                        answer_parts.append(content)
                    yield _sse_event(event_name, data or {})
                elif event_name == "done":
                    final_response = data if isinstance(data, TravelQAResponse) else TravelQAResponse.model_validate(data)
                else:
                    yield _sse_event(event_name, data or {})

            if final_response is None:
                final_response = TravelQAResponse(answer="".join(answer_parts))
            final_response = _persist_qa_exchange(resource_qa_store, conversation, request.question, final_response)
            yield _sse_event("done", final_response.model_dump(mode="json"))
        except Exception as exc:
            logger.warning("Travel QA stream failed: %s", exc)
            yield _sse_event("error", {"message": str(exc)})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _prepare_qa_memory(resources: AppResources, request: TravelQARequest):
    resource_qa_store = getattr(resources, "qa_store", None)
    conversation = None
    conversation_history: list[dict[str, str]] = []
    if resource_qa_store is None:
        return resource_qa_store, conversation, conversation_history
    try:
        conversation = resource_qa_store.get_or_create_conversation(
            conversation_id=request.conversation_id,
            user_id=request.user_id,
            anonymous_id=request.anonymous_id,
            title=request.question,
        )
        conversation_history = resource_qa_store.get_recent_messages(conversation["id"], limit=8)
    except Exception as exc:
        logger.warning("QA conversation memory read failed, answering without history: %s", exc)
        conversation = None
        conversation_history = []
    return resource_qa_store, conversation, conversation_history


def _persist_qa_exchange(resource_qa_store, conversation, question: str, result: TravelQAResponse) -> TravelQAResponse:
    if conversation is None or resource_qa_store is None:
        return result
    message_id = None
    try:
        resource_qa_store.append_message(conversation["id"], "user", question)
        assistant_message = resource_qa_store.append_message(
            conversation["id"],
            "assistant",
            result.answer,
            sources=result.sources,
            retrieved_count=result.retrieved_count,
            generation_mode=result.generation_mode,
        )
        message_id = assistant_message.get("id") if isinstance(assistant_message, dict) else None
    except Exception as exc:
        logger.warning("QA conversation memory write failed: %s", exc)
    return result.model_copy(update={"conversation_id": conversation["id"], "message_id": message_id})


def _response_to_stream_events(response: TravelQAResponse):
    for chunk in _chunk_text(response.answer):
        yield {"event": "answer_delta", "data": {"content": chunk}}
    yield {"event": "done", "data": response}


def _chunk_text(text: str, size: int = 12):
    for index in range(0, len(text), size):
        yield text[index : index + size]


def _sse_event(event: str, data) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


@app.get("/api/qa/conversations", response_model=ApiResponse[list[TravelQAConversationSummary]])
def list_qa_conversations(
    user_id: str | None = Query(default=None, max_length=120),
    anonymous_id: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=50, ge=1, le=100),
):
    resources = get_app_resources()
    resource_qa_store = getattr(resources, "qa_store", None)
    if resource_qa_store is None:
        return ApiResponse[list[TravelQAConversationSummary]](success=True, message="问答记忆未启用", data=[])
    try:
        conversations = resource_qa_store.list_conversations(user_id=user_id, anonymous_id=anonymous_id, limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"问答会话查询失败: {exc}") from exc
    return ApiResponse[list[TravelQAConversationSummary]](success=True, message="问答会话列表获取成功", data=conversations)


@app.get("/api/qa/conversations/{conversation_id}", response_model=ApiResponse[TravelQAConversationDetail])
def get_qa_conversation(conversation_id: str):
    resources = get_app_resources()
    resource_qa_store = getattr(resources, "qa_store", None)
    if resource_qa_store is None:
        raise HTTPException(status_code=503, detail="问答记忆未启用")
    try:
        conversation = resource_qa_store.get_conversation(conversation_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"问答会话查询失败: {exc}") from exc
    if conversation is None:
        raise HTTPException(status_code=404, detail="问答会话不存在")
    return ApiResponse[TravelQAConversationDetail](success=True, message="问答会话详情获取成功", data=conversation)


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
def get_poi_photo(
    name: str = Query(..., min_length=1, max_length=120),
    city: str = Query(default="", max_length=40),
    report_id: str | None = Query(default=None, max_length=80),
):
    resources = get_app_resources()
    cache_key = asset_cache_key("attraction_image", city, name)
    photo_url = _cached_asset_value(resources.report_store, "attraction_image", cache_key)
    if not photo_url:
        query = f"{city} {name} China landmark".strip() if city else f"{name} China landmark"
        photo_url = _valid_cached_url(resources.image_provider.image_for(query))
        if photo_url:
            _cache_asset(
                resources.report_store,
                "attraction_image",
                cache_key,
                city,
                name,
                photo_url,
                response_payload={"photo_url": photo_url},
            )
    _write_report_attraction_image(report_id, resources.report_store, name, photo_url)
    return {
        "success": True,
        "message": "获取图片成功",
        "data": {
            "name": name,
            "photo_url": photo_url,
        },
    }


@app.get("/api/map/poi")
def search_map_poi(
    keywords: str = Query(..., min_length=1, max_length=120),
    city: str = Query(default="北京", min_length=1, max_length=40),
    limit: int = Query(default=10, ge=1, le=30),
):
    resources = get_app_resources()
    cache_key = asset_cache_key("map_poi", city, keywords, str(limit))
    cached_data = _cached_asset_payload(resources.report_store, "map_poi", cache_key)
    if isinstance(cached_data, list):
        return {
            "success": True,
            "message": "POI鎼滅储鎴愬姛",
            "data": cached_data,
        }
    pois = resources.orchestrator.amap.search_pois(city=city, keywords=[keywords], limit=limit)
    data = [poi.model_dump(mode="json") for poi in pois]
    _cache_asset(resources.report_store, "map_poi", cache_key, city, keywords, "cached", response_payload=data)
    return {
        "success": True,
        "message": "POI搜索成功",
        "data": data,
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
