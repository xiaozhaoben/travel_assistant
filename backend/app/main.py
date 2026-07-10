from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import re
import threading
import uuid
from urllib.parse import urlparse

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from .core.config import get_settings
from .core.logging_config import setup_logging
from .core.llm_service import create_llm
from .domain.models import (
    ApiResponse,
    AuthTokenResponse,
    MergeAnonymousRequest,
    PlanEditRequest,
    PrincipalTokenResponse,
    TravelDocumentAutoIngestRequest,
    TravelDocumentIngestJobResponse,
    TravelDocumentIngestJobStatus,
    TravelDocumentIngestRequest,
    TravelDocumentIngestResult,
    TravelDocumentUrlIngestRequest,
    TravelDocumentSearchRequest,
    TravelDocumentSearchResponse,
    TravelDocumentSearchResult,
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
    UserLoginRequest,
    UserRegisterRequest,
)
from .auth.service import (
    configure_auth,
    create_access_token,
    create_user,
    ensure_users_table,
    get_auth_connections,
    get_current_user,
    get_user_by_username,
    hash_password,
    merge_anonymous_conversations,
    verify_password,
)
from .auth.principal import (
    Principal,
    configure_principal_auth,
    create_principal_token,
    get_current_principal,
)
from .core.api_errors import install_api_error_handlers
from .integrations.services import UnsplashMCPClient
from .knowledge.news_agent import TravelNewsIngestionAgent, configured_travel_feeds
from .knowledge.prompts import render_travel_document_metadata_prompt
from .knowledge.qa_agent import TravelQuestionAnsweringAgent
from .knowledge.qa_checkpointer import create_qa_checkpointer
from .knowledge.vector_store import create_travel_vector_store, normalize_document_content
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
    qa_checkpointer: object | None
    news_agent: TravelNewsIngestionAgent
    qa_agent: TravelQuestionAnsweringAgent
    image_provider: UnsplashMCPClient


@dataclass
class KnowledgeIngestJob:
    job_id: str
    status: str
    message: str
    source_type: str
    result: dict | None
    error: str | None
    created_at: datetime
    updated_at: datetime


orchestrator: TravelAgentOrchestrator | None = None
report_store = None
travel_vector_store = None
qa_store = None
qa_checkpointer = None
news_agent: TravelNewsIngestionAgent | None = None
qa_agent: TravelQuestionAnsweringAgent | None = None
image_provider: UnsplashMCPClient | None = None
knowledge_ingest_jobs: dict[str, KnowledgeIngestJob] = {}
knowledge_ingest_jobs_lock = threading.Lock()


def create_app_resources() -> AppResources:
    resource_settings = get_settings()
    resource_report_store = create_report_store(resource_settings.database_url)
    resource_vector_store = create_travel_vector_store(resource_settings.database_url)
    resource_qa_store = create_qa_conversation_store(resource_settings.database_url)
    resource_qa_checkpointer = create_qa_checkpointer(resource_settings.database_url)
    resource_orchestrator = TravelAgentOrchestrator()
    resource_orchestrator.configure_reflection_memory(resource_vector_store)
    resource_news_agent = TravelNewsIngestionAgent(resource_vector_store)
    resource_qa_agent = TravelQuestionAnsweringAgent(
        resource_vector_store,
        llm=resource_orchestrator.planner.llm,
        amap_client=resource_orchestrator.amap,
        checkpointer=resource_qa_checkpointer,
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
        qa_checkpointer=resource_qa_checkpointer,
        news_agent=resource_news_agent,
        qa_agent=resource_qa_agent,
        image_provider=resource_image_provider,
    )


def bind_app_resources(resources: AppResources) -> None:
    global orchestrator, report_store, travel_vector_store, qa_store, qa_checkpointer, news_agent, qa_agent, image_provider
    orchestrator = resources.orchestrator
    report_store = resources.report_store
    travel_vector_store = resources.travel_vector_store
    qa_store = resources.qa_store
    qa_checkpointer = resources.qa_checkpointer
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
        qa_checkpointer=qa_checkpointer,
        news_agent=news_agent or TravelNewsIngestionAgent(travel_vector_store),
        qa_agent=qa_agent or TravelQuestionAnsweringAgent(
            travel_vector_store,
            llm=orchestrator.planner.llm,
            amap_client=orchestrator.amap,
            checkpointer=qa_checkpointer,
        ),
        image_provider=image_provider or UnsplashMCPClient(),
    )


def get_app_resources() -> AppResources:
    state_resources = getattr(app.state, "resources", None)
    global_resources = current_global_resources()
    if state_resources is not None and (
        (news_agent is not None and news_agent is not state_resources.news_agent)
        or (qa_agent is not None and qa_agent is not state_resources.qa_agent)
        or (qa_store is not None and qa_store is not state_resources.qa_store)
        or (qa_checkpointer is not None and qa_checkpointer is not state_resources.qa_checkpointer)
        or (image_provider is not None and image_provider is not state_resources.image_provider)
    ):
        resources = AppResources(
            orchestrator=orchestrator or state_resources.orchestrator,
            report_store=report_store if report_store is not None else state_resources.report_store,
            travel_vector_store=travel_vector_store
            if travel_vector_store is not None
            else state_resources.travel_vector_store,
            qa_store=qa_store if qa_store is not None else state_resources.qa_store,
            qa_checkpointer=qa_checkpointer if qa_checkpointer is not None else state_resources.qa_checkpointer,
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
            or (
                qa_checkpointer is not None
                and state_resources.qa_checkpointer is not global_resources.qa_checkpointer
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
            qa_store=qa_store if qa_store is not None else base_resources.qa_store,
            qa_checkpointer=qa_checkpointer if qa_checkpointer is not None else base_resources.qa_checkpointer,
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
    configure_principal_auth(settings.jwt_secret_key, settings.jwt_algorithm)
    resources = current_global_resources() or create_app_resources()
    bind_app_resources(resources)
    fastapi_app.state.resources = resources
    qa_store_for_auth = getattr(resources, "qa_store", None)
    if qa_store_for_auth is not None and hasattr(qa_store_for_auth, "connections"):
        ensure_users_table(qa_store_for_auth.connections)
        configure_auth(qa_store_for_auth.connections, settings.jwt_secret_key, settings.jwt_algorithm)
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
        resources.qa_checkpointer,
        resources.image_provider,
        resources.orchestrator.amap,
        resources.orchestrator.unsplash,
    ):
        close = getattr(candidate, "close", None)
        if callable(close):
            close()
        connection_pool = getattr(candidate, "conn", None)
        pool_close = getattr(connection_pool, "close", None)
        if callable(pool_close):
            pool_close()


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
install_api_error_handlers(app)

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
def ask_travel_question(
    request: TravelQARequest,
    principal: Principal = Depends(get_current_principal),
):
    resources = get_app_resources()
    user_id, anonymous_id = _apply_auth_identity(principal)
    resource_qa_store, conversation, conversation_history = _prepare_qa_memory(
        resources,
        request,
        user_id=user_id,
        anonymous_id=anonymous_id,
    )
    config = _qa_thread_config(
        conversation["id"] if conversation else None,
        user_id=user_id,
    )

    result = _call_qa_agent(
        resources.qa_agent,
        request.question,
        request.top_k,
        conversation_history,
        config,
    )
    result = _persist_qa_exchange(resource_qa_store, conversation, request.question, result)
    return ApiResponse[TravelQAResponse](success=True, message="智能问答完成", data=result)


@app.post("/api/qa/ask/stream")
def stream_travel_question(
    request: TravelQARequest,
    principal: Principal = Depends(get_current_principal),
):
    resources = get_app_resources()
    user_id, anonymous_id = _apply_auth_identity(principal)
    resource_qa_store, conversation, conversation_history = _prepare_qa_memory(
        resources,
        request,
        user_id=user_id,
        anonymous_id=anonymous_id,
    )
    config = _qa_thread_config(
        conversation["id"] if conversation else None,
        user_id=user_id,
    )

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
                events = _stream_qa_agent(
                    resources.qa_agent,
                    request.question,
                    request.top_k,
                    conversation_history,
                    config,
                )
            else:
                events = _response_to_stream_events(
                    _call_qa_agent(
                        resources.qa_agent,
                        request.question,
                        request.top_k,
                        conversation_history,
                        config,
                    )
                )

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


def _prepare_qa_memory(
    resources: AppResources,
    request: TravelQARequest,
    user_id: str | None,
    anonymous_id: str | None,
):
    resource_qa_store = getattr(resources, "qa_store", None)
    conversation = None
    conversation_history: list[dict[str, str]] = []
    if resource_qa_store is None:
        return resource_qa_store, conversation, conversation_history
    try:
        conversation = resource_qa_store.get_or_create_conversation(
            conversation_id=request.conversation_id,
            user_id=user_id,
            anonymous_id=anonymous_id,
            title=request.question,
        )
        conversation_history = resource_qa_store.get_recent_messages(conversation["id"], limit=8)
    except Exception as exc:
        logger.warning("QA conversation memory read failed, answering without history: %s", exc)
        conversation = None
        conversation_history = []
    return resource_qa_store, conversation, conversation_history


def _qa_thread_config(thread_id: str | None, user_id: str | None = None) -> dict[str, dict[str, str]] | None:
    configurable: dict[str, str] = {}
    if thread_id:
        configurable["thread_id"] = thread_id
    if user_id:
        configurable["user_id"] = user_id
    if not configurable:
        return None
    return {"configurable": configurable}


def _apply_auth_identity(principal: Principal) -> tuple[str | None, str | None]:
    return principal.user_id, principal.anonymous_id


def _call_qa_agent(qa_agent_instance, question: str, top_k: int, conversation_history: list[dict[str, str]], config: dict | None):
    try:
        return qa_agent_instance.ask(
            question,
            top_k=top_k,
            conversation_history=conversation_history,
            config=config,
        )
    except TypeError:
        return qa_agent_instance.ask(question, top_k=top_k, conversation_history=conversation_history)


def _stream_qa_agent(
    qa_agent_instance,
    question: str,
    top_k: int,
    conversation_history: list[dict[str, str]],
    config: dict | None,
):
    try:
        return qa_agent_instance.stream(
            question,
            top_k=top_k,
            conversation_history=conversation_history,
            config=config,
        )
    except TypeError:
        return qa_agent_instance.stream(question, top_k=top_k, conversation_history=conversation_history)


def _persist_qa_exchange(resource_qa_store, conversation, question: str, result: TravelQAResponse) -> TravelQAResponse:
    if conversation is None or resource_qa_store is None:
        logger.info("QA persist skipped: conversation=%s, qa_store=%s", conversation is not None, resource_qa_store is not None)
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
            used_web_search=result.used_web_search,
        )
        message_id = assistant_message.get("id") if isinstance(assistant_message, dict) else None
        logger.info("QA exchange persisted: conversation=%s, message_id=%s, answer_len=%d", conversation["id"], message_id, len(result.answer))
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


@app.post("/api/knowledge/documents", response_model=ApiResponse[TravelDocumentIngestResult])
def ingest_travel_document(request: TravelDocumentIngestRequest):
    store = travel_vector_store
    if store is None:
        resources = get_app_resources()
        store = resources.travel_vector_store
    if store is None:
        raise HTTPException(status_code=503, detail="Travel knowledge vector store is not configured.")
    try:
        result = store.ingest_document(
            title=request.title,
            content=request.content,
            source_name=request.source_name,
            source_url=request.source_url,
            source_type=request.source_type,
            publish_date=request.publish_date,
            province=request.province,
            city=request.city,
            data_type=request.data_type,
            scenic_spot=request.scenic_spot,
            metadata=request.metadata,
        )
    except Exception as exc:
        logger.warning("Travel document ingest failed: %s", exc)
        raise HTTPException(status_code=503, detail=f"Travel document ingest failed: {exc}") from exc
    return ApiResponse[TravelDocumentIngestResult](
        success=True,
        message="旅行文档入库完成",
        data=TravelDocumentIngestResult.model_validate(result),
    )


@app.post("/api/knowledge/documents/from-url", response_model=ApiResponse[TravelDocumentIngestResult])
def ingest_travel_document_from_url(request: TravelDocumentUrlIngestRequest):
    store = travel_vector_store
    if store is None:
        resources = get_app_resources()
        store = resources.travel_vector_store
    if store is None:
        raise HTTPException(status_code=503, detail="Travel knowledge vector store is not configured.")
    try:
        import httpx

        response = httpx.get(
            request.source_url,
            follow_redirects=True,
            timeout=httpx.Timeout(20.0, connect=8.0),
            headers={
                "User-Agent": "travel-assistant/1.0 (+https://localhost)",
                "Accept": "text/html, text/plain, application/xhtml+xml, */*",
            },
        )
        response.raise_for_status()
        title = request.title or extract_html_title(response.text) or title_from_url(request.source_url)
        content = normalize_document_content(response.text)
        if not content:
            raise ValueError("Fetched URL did not contain readable text.")
        inferred = infer_travel_document_metadata(
            content,
            {
                "title": title,
                "source_url": request.source_url,
                "source_name": request.source_name,
                "source_type": request.source_type or "web",
            },
        )
        result = store.ingest_document(
            title=inferred["title"],
            content=content,
            source_name=inferred["source_name"],
            source_url=request.source_url,
            source_type=inferred["source_type"],
            publish_date=inferred.get("publish_date") or request.publish_date,
            province=inferred.get("province") or request.province,
            city=inferred.get("city") or request.city,
            data_type=inferred.get("data_type") or request.data_type,
            scenic_spot=inferred.get("scenic_spot") or request.scenic_spot,
            metadata={**request.metadata, **dict(inferred.get("metadata") or {}), "ingest_method": "url"},
        )
    except Exception as exc:
        logger.warning("Travel document URL ingest failed: %s", exc)
        raise HTTPException(status_code=503, detail=f"Travel document URL ingest failed: {exc}") from exc
    return ApiResponse[TravelDocumentIngestResult](
        success=True,
        message="网页旅行文档入库完成",
        data=TravelDocumentIngestResult.model_validate(result),
    )


@app.post("/api/knowledge/documents/auto", response_model=ApiResponse[TravelDocumentIngestResult])
def ingest_travel_document_auto(request: TravelDocumentAutoIngestRequest):
    store = travel_vector_store
    if store is None:
        resources = get_app_resources()
        store = resources.travel_vector_store
    if store is None:
        raise HTTPException(status_code=503, detail="Travel knowledge vector store is not configured.")
    try:
        content = normalize_document_content(request.content)
        if not content:
            raise ValueError("Uploaded file did not contain readable text.")
        inferred = infer_travel_document_metadata(
            content,
            {
                "title": request.file_name or title_from_url(request.source_url or ""),
                "source_url": request.source_url,
                "source_name": request.file_name or "上传文件",
                "source_type": request.source_type or "upload",
            },
        )
        result = store.ingest_document(
            title=inferred["title"],
            content=content,
            source_name=inferred["source_name"],
            source_url=request.source_url,
            source_type=inferred["source_type"],
            publish_date=inferred.get("publish_date"),
            province=inferred.get("province"),
            city=inferred.get("city"),
            data_type=inferred.get("data_type"),
            scenic_spot=inferred.get("scenic_spot"),
            metadata={**dict(inferred.get("metadata") or {}), "ingest_method": request.source_type or "upload"},
        )
    except Exception as exc:
        logger.warning("Travel document auto ingest failed: %s", exc)
        raise HTTPException(status_code=503, detail=f"Travel document auto ingest failed: {exc}") from exc
    return ApiResponse[TravelDocumentIngestResult](
        success=True,
        message="旅行文档自动解析入库完成",
        data=TravelDocumentIngestResult.model_validate(result),
    )


def create_knowledge_ingest_job(source_type: str, message: str) -> KnowledgeIngestJob:
    now = datetime.now(timezone.utc)
    job = KnowledgeIngestJob(
        job_id=uuid.uuid4().hex,
        status="queued",
        message=message,
        source_type=source_type,
        result=None,
        error=None,
        created_at=now,
        updated_at=now,
    )
    with knowledge_ingest_jobs_lock:
        knowledge_ingest_jobs[job.job_id] = job
    return job


def update_knowledge_ingest_job(job_id: str, **changes) -> KnowledgeIngestJob | None:
    with knowledge_ingest_jobs_lock:
        job = knowledge_ingest_jobs.get(job_id)
        if job is None:
            return None
        for key, value in changes.items():
            setattr(job, key, value)
        job.updated_at = datetime.now(timezone.utc)
        return job


def knowledge_ingest_job_status(job: KnowledgeIngestJob) -> TravelDocumentIngestJobStatus:
    result = TravelDocumentIngestResult.model_validate(job.result) if job.result else None
    return TravelDocumentIngestJobStatus(
        job_id=job.job_id,
        status=job.status,
        message=job.message,
        result=result,
        error=job.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def run_knowledge_ingest_job(job_id: str, request, runner) -> None:
    update_knowledge_ingest_job(job_id, status="running", message="正在解析并写入向量库")
    try:
        response = runner(request)
        result = getattr(response, "data", None)
        result_payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
        update_knowledge_ingest_job(
            job_id,
            status="completed",
            message="入库完成",
            result=result_payload,
            error=None,
        )
    except HTTPException as exc:
        update_knowledge_ingest_job(job_id, status="failed", message="入库失败", error=str(exc.detail))
    except Exception as exc:  # pragma: no cover - defensive background boundary
        logger.exception("Knowledge ingest job failed: %s", exc)
        update_knowledge_ingest_job(job_id, status="failed", message="入库失败", error=str(exc))


@app.post("/api/knowledge/documents/from-url/jobs", response_model=ApiResponse[TravelDocumentIngestJobResponse])
def create_travel_document_url_ingest_job(request: TravelDocumentUrlIngestRequest, background_tasks: BackgroundTasks):
    job = create_knowledge_ingest_job("url", "网页入库任务已创建")
    background_tasks.add_task(run_knowledge_ingest_job, job.job_id, request, ingest_travel_document_from_url)
    return ApiResponse[TravelDocumentIngestJobResponse](
        success=True,
        message=job.message,
        data=TravelDocumentIngestJobResponse(job_id=job.job_id, status=job.status, message=job.message),
    )


@app.post("/api/knowledge/documents/auto/jobs", response_model=ApiResponse[TravelDocumentIngestJobResponse])
def create_travel_document_auto_ingest_job(request: TravelDocumentAutoIngestRequest, background_tasks: BackgroundTasks):
    job = create_knowledge_ingest_job("upload", "文件入库任务已创建")
    background_tasks.add_task(run_knowledge_ingest_job, job.job_id, request, ingest_travel_document_auto)
    return ApiResponse[TravelDocumentIngestJobResponse](
        success=True,
        message=job.message,
        data=TravelDocumentIngestJobResponse(job_id=job.job_id, status=job.status, message=job.message),
    )


@app.get("/api/knowledge/documents/jobs/{job_id}", response_model=ApiResponse[TravelDocumentIngestJobStatus])
def get_travel_document_ingest_job(job_id: str):
    with knowledge_ingest_jobs_lock:
        job = knowledge_ingest_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Knowledge ingest job not found.")
    return ApiResponse[TravelDocumentIngestJobStatus](
        success=True,
        message=job.message,
        data=knowledge_ingest_job_status(job),
    )


@app.post("/api/knowledge/search", response_model=ApiResponse[TravelDocumentSearchResponse])
def search_travel_documents(request: TravelDocumentSearchRequest):
    store = travel_vector_store
    if store is None:
        resources = get_app_resources()
        store = resources.travel_vector_store
    if store is None:
        raise HTTPException(status_code=503, detail="Travel knowledge vector store is not configured.")
    try:
        results = store.search_chunks(
            query=request.query,
            top_k=request.top_k,
            province=request.province,
            city=request.city,
            data_type=request.data_type,
            source_type=request.source_type,
            source_name=request.source_name,
            publish_date_from=request.publish_date_from,
            publish_date_to=request.publish_date_to,
        )
    except Exception as exc:
        logger.warning("Travel document search failed: %s", exc)
        raise HTTPException(status_code=503, detail=f"Travel document search failed: {exc}") from exc
    payload = TravelDocumentSearchResponse(
        query=request.query,
        results=[normalize_document_search_result(item) for item in results],
    )
    return ApiResponse[TravelDocumentSearchResponse](success=True, message="旅行文档检索完成", data=payload)


def normalize_document_search_result(item) -> TravelDocumentSearchResult:
    if isinstance(item, dict):
        payload = dict(item)
    else:
        payload = {
            "chunk_id": getattr(item, "chunk_id", ""),
            "title": getattr(item, "title", ""),
            "section": getattr(item, "section", ""),
            "content": getattr(item, "content", ""),
            "source_name": getattr(item, "source_name", ""),
            "source_url": getattr(item, "source_url", None),
            "publish_date": getattr(item, "publish_date", None),
            "score": getattr(item, "score", 0.0),
            "metadata": getattr(item, "metadata", None) or {},
        }
    payload["metadata"] = payload.get("metadata") or {}
    return TravelDocumentSearchResult.model_validate(payload)


DOCUMENT_DATA_TYPES = (
    "城市旅游介绍",
    "景点信息",
    "交通信息",
    "旅游政策公告",
    "平台旅游趋势报告",
    "文旅数据",
    "行程路线推荐",
)


def infer_travel_document_metadata(content: str, hints: dict[str, object] | None = None) -> dict[str, object]:
    hints = hints or {}
    fallback = fallback_travel_document_metadata(content, hints)
    llm = create_llm()
    if llm is None:
        return fallback
    prompt = render_travel_document_metadata_prompt(
        content=content,
        hints=hints,
        data_types=DOCUMENT_DATA_TYPES,
    )
    try:
        response = llm.invoke(prompt)
        parsed = parse_json_object(str(getattr(response, "content", response)))
        return normalize_inferred_metadata(parsed, fallback)
    except Exception as exc:
        logger.warning("Travel document metadata inference failed, using fallback: %s", exc)
        return fallback


def fallback_travel_document_metadata(content: str, hints: dict[str, object]) -> dict[str, object]:
    source_url = str(hints.get("source_url") or "")
    title_hint = str(hints.get("title") or "").strip()
    source_name_hint = str(hints.get("source_name") or "").strip()
    source_type = str(hints.get("source_type") or "upload").strip() or "upload"
    title = title_hint or title_from_url(source_url) or "旅行文档"
    source_name = source_name_hint or urlparse(source_url).netloc or "上传文件"
    return {
        "title": title[:240],
        "source_name": source_name[:160],
        "source_type": source_type[:80],
        "publish_date": extract_first_date(content),
        "province": None,
        "city": None,
        "scenic_spot": None,
        "data_type": infer_data_type(content),
        "metadata": {"inference": "fallback"},
    }


def normalize_inferred_metadata(parsed: dict[str, object], fallback: dict[str, object]) -> dict[str, object]:
    data = dict(fallback)
    for key in ("title", "source_name", "source_type", "publish_date", "province", "city", "scenic_spot", "data_type"):
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            data[key] = value.strip()
    if data.get("data_type") not in DOCUMENT_DATA_TYPES:
        data["data_type"] = fallback.get("data_type")
    metadata = parsed.get("metadata")
    if isinstance(metadata, dict):
        data["metadata"] = {**dict(fallback.get("metadata") or {}), **metadata, "inference": "llm"}
    else:
        data["metadata"] = {**dict(fallback.get("metadata") or {}), "inference": "llm"}
    return data


def parse_json_object(text: str) -> dict[str, object]:
    try:
        value = json.loads(text.strip())
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("Metadata inference response is not a JSON object.")
    return value


def infer_data_type(content: str) -> str:
    text = content[:3000]
    if any(word in text for word in ("政策", "公告", "通知", "预约", "开放时间")):
        return "旅游政策公告"
    if any(word in text for word in ("交通", "地铁", "机场", "高铁", "公交")):
        return "交通信息"
    if any(word in text for word in ("路线", "行程", "几日游", "一日游")):
        return "行程路线推荐"
    if any(word in text for word in ("趋势", "报告", "数据", "游客量")):
        return "平台旅游趋势报告"
    if any(word in text for word in ("景区", "景点", "博物馆", "公园")):
        return "景点信息"
    return "城市旅游介绍"


def extract_first_date(content: str) -> str | None:
    match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", content)
    if not match:
        return None
    year, month, day = match.groups()
    return f"{year}-{int(month):02d}-{int(day):02d}"


def extract_html_title(html_text: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html_text or "", flags=re.I | re.S)
    if not match:
        return ""
    return normalize_document_content(match.group(1))[:240]


def title_from_url(url: str) -> str:
    parsed = urlparse(url)
    path_name = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    return path_name or parsed.netloc or "网页旅行文档"


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


# ---------------------------------------------------------------------------
# 认证端点
# ---------------------------------------------------------------------------


@app.post("/api/auth/anonymous", response_model=ApiResponse[PrincipalTokenResponse])
def create_anonymous_principal():
    subject = str(uuid.uuid4())
    token = create_principal_token(
        subject=subject,
        principal_type="anonymous",
        username="",
        secret=settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
        expire_minutes=settings.anonymous_jwt_expire_minutes,
    )
    return ApiResponse[PrincipalTokenResponse](
        success=True,
        message="匿名身份创建成功",
        data=PrincipalTokenResponse(
            access_token=token,
            principal_type="anonymous",
            subject=subject,
            expires_in=settings.anonymous_jwt_expire_minutes * 60,
        ),
    )


@app.post("/api/auth/register", response_model=ApiResponse[AuthTokenResponse])
def register_user(request: UserRegisterRequest):
    connections = get_auth_connections()
    existing = get_user_by_username(connections, request.username)
    if existing is not None:
        raise HTTPException(status_code=409, detail="用户名已存在")
    user = create_user(connections, request.username, hash_password(request.password))
    token = create_access_token(
        user["id"],
        user["username"],
        settings.jwt_secret_key,
        settings.jwt_algorithm,
        settings.jwt_expire_minutes,
    )
    return ApiResponse[AuthTokenResponse](
        success=True,
        message="注册成功",
        data=AuthTokenResponse(
            access_token=token,
            user_id=user["id"],
            username=user["username"],
        ),
    )


@app.post("/api/auth/login", response_model=ApiResponse[AuthTokenResponse])
def login_user(request: UserLoginRequest):
    connections = get_auth_connections()
    user = get_user_by_username(connections, request.username)
    if user is None or not verify_password(request.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = create_access_token(
        user["id"],
        user["username"],
        settings.jwt_secret_key,
        settings.jwt_algorithm,
        settings.jwt_expire_minutes,
    )
    return ApiResponse[AuthTokenResponse](
        success=True,
        message="登录成功",
        data=AuthTokenResponse(
            access_token=token,
            user_id=user["id"],
            username=user["username"],
        ),
    )


@app.get("/api/auth/me")
def get_current_auth_user(current_user: dict = Depends(get_current_user)):
    return {
        "success": True,
        "message": "当前用户信息",
        "data": {"user_id": current_user["user_id"], "username": current_user["username"]},
    }


@app.post("/api/auth/merge-anonymous")
def merge_anonymous_sessions(
    request: MergeAnonymousRequest,
    current_user: dict = Depends(get_current_user),
):
    connections = get_auth_connections()
    merged_count = merge_anonymous_conversations(connections, current_user["user_id"], request.anonymous_id)
    return {
        "success": True,
        "message": f"已合并 {merged_count} 个匿名会话",
        "data": {"merged_count": merged_count},
    }
