from __future__ import annotations

import json
import logging
import re
import time
from datetime import timedelta
from typing import Any, List

from langchain.agents import create_agent

try:
    from langchain_core.messages import HumanMessage, SystemMessage
except Exception:  # pragma: no cover - optional until dependencies are installed
    HumanMessage = None
    SystemMessage = None

from app.core.config import get_settings
from app.core.llm_service import create_llm
from app.core.logging_config import log_agent_event
from app.domain.models import (
    Attraction,
    DayPlan,
    Hotel,
    Meal,
    ResearchSnippet,
    TripPlan,
    TripPlanOption,
    TripPlanningResult,
    TripPlanRequest,
    TravelRequirement,
)
from app.integrations.services import AmapMCPClient, BudgetCalculator, TravelRequirementParser, UnsplashMCPClient
from app.prompts.agent_prompts import AgentPrompts
from app.researching.research import DestinationResearchService
from app.storage.plan_log import elapsed_ms, record_llm_call

logger = logging.getLogger(__name__)


def _safe_create_agent(model: Any | None, tools: list[Any], system_prompt: str, name: str):
    if model is None:
        return None
    try:
        return create_agent(
            model=model,
            tools=tools,
            system_prompt=system_prompt,
            name=name,
        )
    except Exception as exc:
        logger.warning("LangChain create_agent failed for %s: %s", name, exc)
        return None


def _extract_message_content(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text") or block.get("content")
                if text:
                    parts.append(str(text))
            elif block:
                parts.append(str(block))
        return "".join(parts)
    return str(content)


def _extract_agent_content(response: Any) -> str:
    if isinstance(response, tuple) and response:
        return _extract_agent_content(response[0])
    if isinstance(response, dict):
        if response.get("messages"):
            return _extract_message_content(response["messages"][-1])
        for value in response.values():
            if isinstance(value, dict) and value.get("messages"):
                return _extract_message_content(value["messages"][-1])
    return _extract_message_content(response)


def _extract_stream_content(chunk: Any) -> str:
    if isinstance(chunk, tuple) and chunk:
        return _extract_stream_content(chunk[0])
    if isinstance(chunk, dict):
        if chunk.get("messages"):
            return _extract_message_content(chunk["messages"][-1])
        for value in chunk.values():
            if isinstance(value, dict) and value.get("messages"):
                return _extract_message_content(value["messages"][-1])
        return ""
    return _extract_message_content(chunk)


def _stream_agent_content(agent: Any, state: dict[str, Any], agent_name: str) -> str:
    start = time.perf_counter()
    first_chunk_seconds: float | None = None
    final_chunk: Any = None
    streamed_parts: list[str] = []
    logger.info("%s LLM stream start", agent_name)
    try:
        try:
            chunks = agent.stream(state, stream_mode="messages")
        except TypeError:
            chunks = agent.stream(state)
        for chunk in chunks:
            final_chunk = chunk
            elapsed = time.perf_counter() - start
            if first_chunk_seconds is None:
                first_chunk_seconds = elapsed
                logger.info("%s LLM stream first_chunk seconds=%.2f", agent_name, elapsed)
            content = _extract_stream_content(chunk)
            if content:
                streamed_parts.append(content)
    except Exception:
        elapsed = time.perf_counter() - start
        record_llm_call(
            component=agent_name,
            operation="stream",
            request_payload=state,
            error="LLM stream failed",
            duration_ms=int(elapsed * 1000),
        )
        logger.info(
            "%s LLM stream failed first_chunk_seconds=%s total_seconds=%.2f",
            agent_name,
            f"{first_chunk_seconds:.2f}" if first_chunk_seconds is not None else "none",
            elapsed,
        )
        raise
    elapsed = time.perf_counter() - start
    content = "".join(streamed_parts) if streamed_parts else _extract_agent_content(final_chunk)
    record_llm_call(
        component=agent_name,
        operation="stream",
        request_payload=state,
        response_payload={"content": content},
        duration_ms=elapsed_ms(start),
    )
    logger.info(
        "%s LLM stream complete first_chunk_seconds=%s total_seconds=%.2f content_chars=%s",
        agent_name,
        f"{first_chunk_seconds:.2f}" if first_chunk_seconds is not None else "none",
        elapsed,
        len(content),
    )
    return content


class AttractionSearchAgent:
    name = "AttractionSearchAgent"

    def __init__(self, amap: AmapMCPClient, unsplash: UnsplashMCPClient, llm: Any | None = None):
        self.amap = amap
        self.unsplash = unsplash
        self.llm = llm
        self.langchain_agent = _safe_create_agent(
            llm,
            [],
            AgentPrompts.ATTRACTION_SEARCH,
            "attraction_search_agent",
        )

    def run(self, requirement: TravelRequirement) -> List[Attraction]:
        search_queries = self._build_search_queries(requirement)
        search_options: dict[str, Any] = {
            "limit": requirement.days * 3,
            "ranking_preferences": requirement.preferences,
        }
        if requirement.must_visit:
            search_options["must_visit"] = requirement.must_visit
        if requirement.avoid_places:
            search_options["avoid_places"] = requirement.avoid_places
        attractions = self.amap.search_pois(requirement.city, search_queries, **search_options)
        return [
            attraction.model_copy(update={"image_url": self.unsplash.image_for(f"{requirement.city} {attraction.name}")})
            for attraction in attractions
        ]

    def _build_search_queries(self, requirement: TravelRequirement) -> List[str]:
        ai_queries = self._try_ai_search_queries(requirement)
        if ai_queries:
            return ai_queries
        return self._fallback_search_queries(requirement)

    def _try_ai_search_queries(self, requirement: TravelRequirement) -> List[str]:
        if self.langchain_agent is None and self.llm is None:
            return []
        prompt = self._build_query_prompt(requirement)
        try:
            if self.langchain_agent is not None:
                content = _stream_agent_content(
                    self.langchain_agent,
                    {"messages": [{"role": "user", "content": prompt}]},
                    self.name,
                )
            else:
                start = time.perf_counter()
                response = self.llm.invoke(prompt)
                content = str(getattr(response, "content", response))
                record_llm_call(
                    component=self.name,
                    operation="invoke",
                    request_payload={"prompt": prompt},
                    response_payload={"content": content},
                    duration_ms=elapsed_ms(start),
                )
            data = self._extract_json(str(content))
            queries = data.get("queries") if isinstance(data, dict) else None
            return self._normalize_search_queries(queries, requirement.city)
        except Exception as exc:
            logger.warning("AttractionSearchAgent query planning failed, using fallback queries: %s", exc)
            return []

    def _build_query_prompt(self, requirement: TravelRequirement) -> str:
        payload = requirement.model_dump(mode="json")
        return (
            "请为高德地图 maps_text_search 生成 8-12 个中文 POI 搜索关键词。"
            "优先输出你判断真实存在、可独立游览、可在地图中直接搜到的具体地点名称，例如陈家祠、沙面岛、南越王博物院、越秀公园这类 POI。"
            "不要输出模板词或泛分类词，例如“广州历史街区”“广州特色街区”“广州城市公园”“广州城市地标”“广州历史文化景点”。"
            "可以根据城市和偏好自由发挥，覆盖不同片区、不同类型和不同强度，但每个关键词都应尽量指向一个真实景点、街区、场馆或景区。"
            "不要只围绕一个大景区生成入口、检票处、服务中心、讲解处、馆内小景点。"
            "如果不确定具体名称，宁可输出更知名的真实地标，不要编造“xx历史街区”这种固定形式。"
            "只返回 JSON：{\"queries\":[\"...\"]}。\n"
            f"{json.dumps(payload, ensure_ascii=False)}"
        )

    def _extract_json(self, content: str) -> dict:
        if "```json" in content:
            content = content.split("```json", 1)[1].split("```", 1)[0]
        elif "```" in content:
            content = content.split("```", 1)[1].split("```", 1)[0]
        else:
            match = re.search(r"\{.*\}", content, re.DOTALL)
            if match:
                content = match.group(0)
        return json.loads(content.strip())

    def _normalize_search_queries(self, queries: Any, city: str) -> List[str]:
        if not isinstance(queries, list):
            return []
        normalized: list[str] = []
        forbidden_terms = ["酒店", "厕所", "停车场", "公交站", "地铁站"]
        for query in queries:
            text = str(query).strip()
            if not text or any(term in text for term in forbidden_terms):
                continue
            if city not in text:
                text = f"{city}{text}"
            if self._is_generic_query(text, city):
                continue
            if text not in normalized:
                normalized.append(text)
            if len(normalized) >= 12:
                break
        return normalized

    def _fallback_search_queries(self, requirement: TravelRequirement) -> List[str]:
        queries: list[str] = self._seed_search_queries(requirement)
        for place in requirement.must_visit:
            queries.append(f"{requirement.city}{place}")
        for preference in requirement.preferences:
            if preference == "历史文化":
                queries.extend(
                    [
                        f"{requirement.city}古镇",
                        f"{requirement.city}故居",
                        f"{requirement.city}博物院",
                        f"{requirement.city}博物馆",
                        f"{requirement.city}文化遗址",
                    ]
                )
            elif preference == "自然风光":
                queries.extend(
                    [
                        f"{requirement.city}风景名胜",
                        f"{requirement.city}公园",
                        f"{requirement.city}山水",
                        f"{requirement.city}海滨",
                    ]
                )
            else:
                queries.extend(
                    [
                        f"{requirement.city}{preference}景点",
                        f"{requirement.city}{preference}路线",
                    ]
                )
        queries.extend([f"{requirement.city}必游景点", f"{requirement.city}风景名胜"])
        unique_queries: list[str] = []
        for query in queries:
            if query and query not in unique_queries and not self._is_generic_query(query, requirement.city):
                unique_queries.append(query)
        return unique_queries[:12]

    def _seed_search_queries(self, requirement: TravelRequirement) -> list[str]:
        fallback = getattr(self.amap, "_fallback_attractions_for_city", None)
        if not callable(fallback):
            return []
        try:
            attractions = fallback(requirement.city)
        except Exception:
            return []
        preferred = [
            attraction
            for attraction in attractions
            if self._matches_preferences(attraction, requirement.preferences)
        ]
        ordered = preferred + [attraction for attraction in attractions if attraction not in preferred]
        return [f"{requirement.city}{attraction.name}" for attraction in ordered[:8]]

    def _matches_preferences(self, attraction: Attraction, preferences: list[str]) -> bool:
        text = f"{attraction.name} {attraction.category} {attraction.description}"
        if not preferences:
            return True
        preference_terms = {
            "历史文化": ["历史", "文化", "博物", "故居", "古", "祠", "遗址", "街区"],
            "自然风光": ["自然", "风景", "公园", "山", "湖", "海", "岛"],
            "美食": ["美食", "小吃", "步行街", "街"],
            "艺术": ["艺术", "美术", "展览", "博物"],
            "购物": ["购物", "商业", "步行街"],
            "休闲": ["休闲", "公园", "街区"],
        }
        for preference in preferences:
            terms = preference_terms.get(preference, [preference])
            if any(term in text for term in terms):
                return True
        return False

    def _is_generic_query(self, query: str, city: str) -> bool:
        if not city or not query.startswith(city):
            return False
        suffix = query[len(city):].strip()
        generic_suffixes = {
            "历史街区",
            "特色街区",
            "城市公园",
            "城市地标",
            "历史文化景点",
            "历史文化路线",
            "自然风光路线",
            "文物古迹",
            "必游景点",
            "风景名胜",
            "观景地",
            "美食街",
        }
        return suffix in generic_suffixes


class WeatherQueryAgent:
    name = "WeatherQueryAgent"

    def __init__(self, amap: AmapMCPClient, llm: Any | None = None):
        self.amap = amap
        self.langchain_agent = _safe_create_agent(
            llm,
            [],
            AgentPrompts.WEATHER_QUERY,
            "weather_query_agent",
        )

    def run(self, requirement: TravelRequirement):
        return self.amap.get_weather(requirement.city, requirement.start_date, requirement.days)


class HotelAgent:
    name = "HotelAgent"

    def __init__(self, amap: AmapMCPClient, llm: Any | None = None):
        self.amap = amap
        self.langchain_agent = _safe_create_agent(
            llm,
            [],
            AgentPrompts.HOTEL,
            "hotel_agent",
        )

    def run(self, requirement: TravelRequirement) -> List[Hotel]:
        return self.amap.search_hotels(requirement.city, requirement.budget_level, limit=3)


class PlannerAgent:
    name = "PlannerAgent"

    def __init__(
        self,
        llm: Any | None = None,
        amap: AmapMCPClient | None = None,
        image_provider: UnsplashMCPClient | None = None,
    ):
        self.llm = llm
        self.amap = amap
        self.image_provider = image_provider or UnsplashMCPClient(
            access_key="",
            pexels_api_key="",
            pixabay_api_key="",
            enable_open_sources=False,
        )
        self.langchain_agent = _safe_create_agent(
            llm,
            [],
            AgentPrompts.PLANNER,
            "planner_agent",
        )

    def run(
        self,
        requirement: TravelRequirement,
        attractions: List[Attraction],
        weather,
        hotels: List[Hotel],
        research_context: List[ResearchSnippet] | None = None,
    ) -> TripPlanningResult:
        research_context = research_context or []
        if self.langchain_agent is not None or self.llm is not None:
            llm_result = self._try_llm_plan(requirement, attractions, weather, hotels, research_context)
            if llm_result is not None:
                return llm_result
        return self._fallback_result(requirement, attractions, weather, hotels, research_context)

    def _try_llm_plan(
        self,
        requirement: TravelRequirement,
        attractions: List[Attraction],
        weather,
        hotels: List[Hotel],
        research_context: List[ResearchSnippet],
    ) -> TripPlanningResult | None:
        prompt = self._build_prompt(requirement, attractions, weather, hotels, research_context)
        try:
            if SystemMessage is not None and HumanMessage is not None:
                messages = [
                    SystemMessage(content="你是专业行程规划专家，只返回符合要求的 JSON。"),
                    HumanMessage(content=prompt),
                ]
            else:
                messages = prompt
            if self.langchain_agent is not None:
                content = _stream_agent_content(
                    self.langchain_agent,
                    {"messages": [{"role": "user", "content": prompt}]},
                    self.name,
                )
            elif self.llm is not None:
                start = time.perf_counter()
                response = self.llm.invoke(messages)
                content = getattr(response, "content", response)
                record_llm_call(
                    component=self.name,
                    operation="invoke",
                    request_payload={"messages": messages},
                    response_payload={"content": content},
                    duration_ms=elapsed_ms(start),
                )
            else:
                return None
            data = self._extract_json(str(content))
            return self._normalize_llm_result(data, requirement, attractions, weather, hotels, research_context)
        except Exception as exc:
            logger.warning("PlannerAgent LLM planning failed, falling back to local planner: %s", exc)
            return None

    def _build_prompt(
        self,
        requirement: TravelRequirement,
        attractions: List[Attraction],
        weather,
        hotels: List[Hotel],
        research_context: List[ResearchSnippet] | None = None,
    ) -> str:
        selected_attractions = attractions[: min(len(attractions), 6)]
        selected_hotels = hotels[: min(len(hotels), 2)]
        research_context = research_context or []
        payload = {
            "user_request": requirement.model_dump(mode="json"),
            "attractions": [
                {
                    "id": item.id,
                    "name": item.name,
                    "category": item.category,
                    "address": item.address,
                    "location": item.location.model_dump(mode="json"),
                    "visit_duration_minutes": item.visit_duration_minutes,
                    "ticket_price": item.ticket_price,
                    "description": item.description[:80],
                }
                for item in selected_attractions
            ],
            "weather": [item.model_dump(mode="json") for item in weather],
            "hotels": [
                {
                    "id": item.id,
                    "name": item.name,
                    "address": item.address,
                    "location": item.location.model_dump(mode="json"),
                    "type": item.type,
                    "rating": item.rating,
                    "nightly_price": item.nightly_price,
                    "description": item.description,
                }
                for item in selected_hotels
            ],
            "research_context": [item.model_dump(mode="json") for item in research_context[:6]],
        }
        return (
            "请根据资料生成完整旅行计划。返回 JSON，顶层字段为 selected_option_id, options, clarifying_suggestions。"
            "options 必须包含 balanced、relaxed、deep_dive 三套方案；每项包含 id,title,style,suitable_for,highlights,tradeoffs,plan。"
            "plan 字段必须匹配 TripPlan：city, days_count, preferences, budget_level, days, weather, budget, map_center, overall_suggestions, agent_trace。"
            "如果用户需求中出现资料列表外但真实存在的目的地，请由你判断并放入对应 day.attractions，提供 name,category,address,location,visit_duration_minutes,description,ticket_price；"
            "每日 summary 中提到的游览地点必须同步出现在该日 attractions，不要只写在文字描述里。"
            "请把 research_context 中的预约、交通、避坑、餐饮信息写入建议或每日摘要；不要 Markdown，不要额外解释。\n"
            f"{json.dumps(payload, ensure_ascii=False)}"
        )

    def _normalize_llm_result(
        self,
        data: dict,
        requirement: TravelRequirement,
        attractions: List[Attraction],
        weather,
        hotels: List[Hotel],
        research_context: List[ResearchSnippet],
    ) -> TripPlanningResult:
        raw_options = data.get("options")
        if not isinstance(raw_options, list) or not raw_options:
            plan_data = self._normalize_llm_data(data, requirement, attractions, weather, hotels)
            plan = TripPlan.model_validate(plan_data).model_copy(update={"generation_mode": "llm"})
            return self._wrap_plan_options(plan, requirement, research_context)

        options: List[TripPlanOption] = []
        variants = self._variant_meta()
        variant_ids = list(variants.keys())
        for index, raw_option in enumerate(raw_options[:3]):
            raw_option = raw_option if isinstance(raw_option, dict) else {}
            option_id = raw_option.get("id") or variant_ids[min(index, len(variant_ids) - 1)]
            meta = variants.get(option_id, variants[variant_ids[min(index, len(variant_ids) - 1)]])
            plan_payload = raw_option.get("plan") if isinstance(raw_option.get("plan"), dict) else raw_option
            plan_data = self._normalize_llm_data(plan_payload, requirement, attractions, weather, hotels)
            plan = TripPlan.model_validate(plan_data).model_copy(update={"generation_mode": "llm"})
            options.append(
                TripPlanOption(
                    id=option_id,
                    title=raw_option.get("title") or meta["title"],
                    style=raw_option.get("style") or meta["style"],
                    suitable_for=raw_option.get("suitable_for") or meta["suitable_for"],
                    highlights=self._ensure_list(raw_option.get("highlights"), meta["highlights"]),
                    tradeoffs=self._ensure_list(raw_option.get("tradeoffs"), meta["tradeoffs"]),
                    plan=plan,
                )
            )
        if len(options) < 3:
            fallback = self._fallback_result(requirement, attractions, weather, hotels, research_context)
            existing = {option.id for option in options}
            options.extend([option for option in fallback.options if option.id not in existing][: 3 - len(options)])
        return TripPlanningResult(
            selected_option_id=data.get("selected_option_id") or options[0].id,
            options=options[:3],
            research_context=research_context,
            clarifying_suggestions=self._clarifying_suggestions(requirement),
        )

    def _extract_json(self, content: str) -> dict:
        if "```json" in content:
            content = content.split("```json", 1)[1].split("```", 1)[0]
        elif "```" in content:
            content = content.split("```", 1)[1].split("```", 1)[0]
        else:
            match = re.search(r"\{.*\}", content, re.DOTALL)
            if match:
                content = match.group(0)
        return json.loads(content.strip())

    def _normalize_llm_data(
        self,
        data: dict,
        requirement: TravelRequirement,
        attractions: List[Attraction],
        weather,
        hotels: List[Hotel],
    ) -> dict:
        normalized = dict(data)
        normalized.setdefault("city", requirement.city)
        normalized.setdefault("days_count", requirement.days)
        normalized.setdefault("preferences", requirement.preferences)
        normalized.setdefault("budget_level", requirement.budget_level)
        normalized["overall_suggestions"] = self._ensure_list(
            normalized.get("overall_suggestions"),
            ["热门景点建议提前预约，并根据天气调整室内外顺序。"],
        )
        normalized["agent_trace"] = [
            AttractionSearchAgent.name,
            WeatherQueryAgent.name,
            HotelAgent.name,
            self.name,
        ]
        normalized["weather"] = self._repair_weather(normalized.get("weather"), weather, requirement.days)

        source_attractions = {item.id: item for item in attractions}
        source_attractions.update({item.name: item for item in attractions})
        normalized_days = []
        raw_days = self._ensure_list(normalized.get("days"), [])
        for index in range(requirement.days):
            raw_day = raw_days[index] if index < len(raw_days) and isinstance(raw_days[index], dict) else {}
            fallback_hotel = hotels[index % len(hotels)]
            raw_hotel = raw_day.get("hotel")
            day_hotel = self._repair_hotel(raw_hotel, fallback_hotel)
            raw_attractions = raw_day.get("attractions") or raw_day.get("activities") or raw_day.get("spots") or []
            repaired_attractions = self._repair_attractions(raw_attractions, attractions, source_attractions, index, requirement.city)
            route_locations = [item["location"] for item in repaired_attractions]
            meals = self._repair_meals(raw_day.get("meals"), requirement.city, requirement.budget_level, requirement.food_preferences, route_locations)
            normalized_days.append(
                {
                    "day_index": raw_day.get("day_index") or raw_day.get("day_number") or index + 1,
                    "date": raw_day.get("date") or (requirement.start_date + timedelta(days=index)).isoformat(),
                    "theme": raw_day.get("theme") or self._theme(requirement.preferences, index),
                    "summary": raw_day.get("summary")
                    or raw_day.get("description")
                    or raw_day.get("suggestion")
                    or f"第 {index + 1} 天围绕{self._theme(requirement.preferences, index)}展开。",
                    "transportation": self._repair_transportation(raw_day.get("transportation") or raw_day.get("transport")),
                    "hotel": day_hotel,
                    "attractions": repaired_attractions,
                    "meals": meals,
                    "route_points": route_locations,
                    "estimated_transport_cost": raw_day.get("estimated_transport_cost") or raw_day.get("transport_cost") or 60,
                }
            )
        normalized["days"] = normalized_days
        normalized["map_center"] = self._repair_location(normalized.get("map_center")) or normalized_days[0]["route_points"][0]
        normalized["budget"] = self._budget_from_day_dicts(normalized_days)
        return normalized

    def _repair_hotel(self, raw_hotel, fallback_hotel: Hotel) -> dict:
        hotel = fallback_hotel.model_dump(mode="json")
        if not isinstance(raw_hotel, dict):
            return hotel
        for key in ("id", "name", "address", "location", "type", "rating", "nightly_price", "description"):
            if raw_hotel.get(key) not in (None, ""):
                hotel[key] = raw_hotel[key]
        return hotel

    def _repair_attractions(self, raw_items, attractions: List[Attraction], source_attractions: dict, day_index: int, city: str) -> list[dict]:
        raw_list = self._ensure_list(raw_items, [])
        repaired = []
        for offset, raw_item in enumerate(raw_list[:3]):
            if isinstance(raw_item, str):
                raw_item = {"name": raw_item}
            if not isinstance(raw_item, dict):
                continue
            key = raw_item.get("id") or raw_item.get("name")
            source = source_attractions.get(key) if key else None
            if source is None and key:
                source = next((item for item in attractions if key in item.name or item.name in key), None)
            fallback_source = attractions[min(day_index * 2 + offset, len(attractions) - 1)] if attractions else None
            if source is None:
                item = self._repair_llm_attraction(raw_item, fallback_source, day_index, offset, city)
                if item is not None:
                    repaired.append(item)
                continue
            item = source.model_dump(mode="json")
            if raw_item.get("description"):
                item["description"] = raw_item["description"]
            if raw_item.get("visit_duration_minutes") or raw_item.get("duration_minutes"):
                item["visit_duration_minutes"] = raw_item.get("visit_duration_minutes") or raw_item.get("duration_minutes")
            if raw_item.get("ticket_price") is not None:
                item["ticket_price"] = raw_item["ticket_price"]
            repaired.append(item)
        if not repaired:
            repaired = [item.model_dump(mode="json") for item in attractions[day_index * 2 : day_index * 2 + 2]]
        return repaired or [item.model_dump(mode="json") for item in attractions[:1]]

    def _repair_llm_attraction(self, raw_item: dict, fallback_source: Attraction | None, day_index: int, offset: int, city: str) -> dict | None:
        name = raw_item.get("name") or raw_item.get("title") or raw_item.get("poi") or raw_item.get("destination")
        if not name:
            return None
        fallback = fallback_source.model_dump(mode="json") if fallback_source is not None else {}
        location = self._repair_location(raw_item.get("location") or raw_item.get("coordinates"))
        if location is None and raw_item.get("longitude") is not None and raw_item.get("latitude") is not None:
            location = self._repair_location({"longitude": raw_item.get("longitude"), "latitude": raw_item.get("latitude")})
        fallback_location = fallback.get("location") or self._city_center_location(city)
        return {
            "id": raw_item.get("id") or f"llm-{day_index + 1}-{offset + 1}",
            "name": str(name),
            "category": raw_item.get("category") or raw_item.get("type") or fallback.get("category") or "模型推荐",
            "address": raw_item.get("address") or fallback.get("address") or f"{city}市内",
            "location": location or fallback_location,
            "visit_duration_minutes": self._coerce_int(
                raw_item.get("visit_duration_minutes") or raw_item.get("duration_minutes") or raw_item.get("duration"),
                fallback.get("visit_duration_minutes", 120),
            ),
            "description": raw_item.get("description") or raw_item.get("summary") or raw_item.get("reason") or fallback.get("description") or f"{name}由大模型根据用户需求推荐。",
            "ticket_price": self._coerce_int(raw_item.get("ticket_price") or raw_item.get("price") or raw_item.get("cost"), fallback.get("ticket_price", 0)),
            "image_url": self._image_for_attraction(city, str(name)),
            "rating": raw_item.get("rating"),
        }

    def _image_for_attraction(self, city: str, name: str) -> str | None:
        if self.image_provider is None:
            return None
        query = f"{city} {name}".strip()
        try:
            return self.image_provider.image_for(query) or None
        except Exception as exc:
            logger.warning("Attraction image search failed for %s: %s", query, exc)
            return None

    def _city_center_location(self, city: str) -> dict:
        if self.amap is not None:
            return self.amap.city_center(city).model_dump(mode="json")
        return AmapMCPClient(api_key="").city_center(city).model_dump(mode="json")

    def _repair_meals(self, raw_items, city: str, budget_level: str = "中等", food_preferences: str = "", route_points=None) -> list[dict]:
        defaults = self._meals(city, budget_level, food_preferences, route_points or [])
        raw_list = self._ensure_list(raw_items, [])
        type_map = {"早餐": "breakfast", "午餐": "lunch", "晚餐": "dinner", "小吃": "snack"}
        meals = []
        for index, default in enumerate(defaults):
            raw = raw_list[index] if index < len(raw_list) and isinstance(raw_list[index], dict) else {}
            meal_type = type_map.get(raw.get("type"), raw.get("type") or default.type)
            has_real_default = bool(default.id or default.location)
            suggestion = default.name if has_real_default else raw.get("name") or raw.get("suggestion") or default.name
            meals.append(
                {
                    "id": raw.get("id") or default.id,
                    "type": meal_type,
                    "name": suggestion,
                    "address": raw.get("address") or default.address,
                    "estimated_cost": raw.get("estimated_cost") or default.estimated_cost,
                    "description": default.description if has_real_default else raw.get("description") or suggestion,
                    "location": raw.get("location") or (default.location.model_dump(mode="json") if default.location else None),
                    "rating": raw.get("rating") if raw.get("rating") is not None else default.rating,
                    "category": raw.get("category") or default.category,
                }
            )
        return meals

    def _repair_weather(self, raw_items, fallback_weather, days_count: int) -> list[dict]:
        raw_list = self._ensure_list(raw_items, [])
        fallback_list = [item.model_dump(mode="json") for item in fallback_weather]
        repaired = []
        for index in range(days_count):
            fallback = fallback_list[index] if index < len(fallback_list) else fallback_list[-1] if fallback_list else {}
            raw = raw_list[index] if index < len(raw_list) and isinstance(raw_list[index], dict) else {}
            day_weather = raw.get("day_weather") or raw.get("weather") or raw.get("condition") or fallback.get("day_weather") or "多云"
            suggestion = (
                raw.get("suggestion")
                or raw.get("travel_suggestion")
                or raw.get("advice")
                or raw.get("tips")
                or fallback.get("suggestion")
                or "根据天气调整室内外景点顺序。"
            )
            repaired.append(
                {
                    "date": raw.get("date") or fallback.get("date"),
                    "day_weather": day_weather,
                    "night_weather": raw.get("night_weather") or raw.get("night") or fallback.get("night_weather") or day_weather,
                    "day_temp": self._coerce_int(raw.get("day_temp") or raw.get("high_temp") or raw.get("temperature"), fallback.get("day_temp", 25)),
                    "night_temp": self._coerce_int(raw.get("night_temp") or raw.get("low_temp"), fallback.get("night_temp", 15)),
                    "wind": raw.get("wind") or fallback.get("wind") or "微风",
                    "suggestion": suggestion,
                }
            )
        return repaired

    def _coerce_int(self, value, fallback: int) -> int:
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            match = re.search(r"-?\d+", value)
            if match:
                return int(match.group(0))
        return fallback

    def _repair_transportation(self, raw_transportation) -> str:
        if isinstance(raw_transportation, str) and raw_transportation.strip():
            return raw_transportation
        if isinstance(raw_transportation, dict):
            parts = [
                raw_transportation.get("mode"),
                raw_transportation.get("description"),
                raw_transportation.get("route"),
                raw_transportation.get("duration"),
            ]
            text = "，".join(str(part) for part in parts if part)
            return text or "公共交通 + 步行"
        if isinstance(raw_transportation, list):
            text = "，".join(str(item) for item in raw_transportation if item)
            return text or "公共交通 + 步行"
        return "公共交通 + 步行"

    def _repair_location(self, raw_location) -> dict | None:
        if isinstance(raw_location, dict):
            longitude = raw_location.get("longitude") or raw_location.get("lng") or raw_location.get("lon")
            latitude = raw_location.get("latitude") or raw_location.get("lat")
            if longitude is not None and latitude is not None:
                return {"longitude": longitude, "latitude": latitude}
        if isinstance(raw_location, (list, tuple)) and len(raw_location) >= 2:
            return {"longitude": raw_location[0], "latitude": raw_location[1]}
        return None

    def _budget_from_day_dicts(self, days: list[dict]) -> dict:
        total_attractions = sum(item.get("ticket_price", 0) for day in days for item in day["attractions"])
        total_hotels = sum(day["hotel"].get("nightly_price", 0) for day in days)
        total_meals = sum(item.get("estimated_cost", 0) for day in days for item in day["meals"])
        total_transportation = sum(day.get("estimated_transport_cost", 0) for day in days)
        return {
            "total_attractions": total_attractions,
            "total_hotels": total_hotels,
            "total_meals": total_meals,
            "total_transportation": total_transportation,
            "total": total_attractions + total_hotels + total_meals + total_transportation,
        }

    def _ensure_list(self, value, fallback: list) -> list:
        if isinstance(value, list):
            return value
        if value is None:
            return fallback
        return [value]

    def _fallback_result(
        self,
        requirement: TravelRequirement,
        attractions: List[Attraction],
        weather,
        hotels: List[Hotel],
        research_context: List[ResearchSnippet],
    ) -> TripPlanningResult:
        options = [
            self._option_from_plan("balanced", self._fallback_plan(requirement, attractions, weather, hotels, "balanced")),
            self._option_from_plan("relaxed", self._fallback_plan(requirement, attractions, weather, hotels, "relaxed")),
            self._option_from_plan("deep_dive", self._fallback_plan(requirement, attractions, weather, hotels, "deep_dive")),
        ]
        return TripPlanningResult(
            selected_option_id="balanced",
            options=options,
            research_context=research_context,
            clarifying_suggestions=self._clarifying_suggestions(requirement),
        )

    def _wrap_plan_options(
        self,
        plan: TripPlan,
        requirement: TravelRequirement,
        research_context: List[ResearchSnippet],
    ) -> TripPlanningResult:
        fallback = self._fallback_result(requirement, plan.days[0].attractions if plan.days else [], plan.weather, [plan.days[0].hotel] if plan.days else [], research_context)
        options = [self._option_from_plan("balanced", plan), *fallback.options[1:]]
        return TripPlanningResult(
            selected_option_id="balanced",
            options=options[:3],
            research_context=research_context,
            clarifying_suggestions=self._clarifying_suggestions(requirement),
        )

    def _option_from_plan(self, option_id: str, plan: TripPlan) -> TripPlanOption:
        meta = self._variant_meta()[option_id]
        return TripPlanOption(
            id=option_id,
            title=meta["title"],
            style=meta["style"],
            suitable_for=meta["suitable_for"],
            highlights=meta["highlights"],
            tradeoffs=meta["tradeoffs"],
            plan=plan,
        )

    def _variant_meta(self) -> dict[str, dict[str, Any]]:
        return {
            "balanced": {
                "title": "经典均衡方案",
                "style": "经典均衡",
                "suitable_for": "第一次到访、希望覆盖代表性景点的旅行者",
                "highlights": ["覆盖城市代表景点", "预算和体力较均衡", "适合多数用户直接采用"],
                "tradeoffs": ["热门景点较多，需要提前预约"],
            },
            "relaxed": {
                "title": "轻松舒适方案",
                "style": "轻松舒适",
                "suitable_for": "亲子、带老人、希望少走路或慢游的旅行者",
                "highlights": ["每天景点更少", "减少跨城折返", "预留更多休息时间"],
                "tradeoffs": ["覆盖景点数量少于均衡方案"],
            },
            "deep_dive": {
                "title": "深度探索方案",
                "style": "深度探索",
                "suitable_for": "体力较好、想深入了解城市主题的人",
                "highlights": ["主题更集中", "加入更多支线景点", "文化体验更完整"],
                "tradeoffs": ["步行和换乘强度更高"],
            },
        }

    def _clarifying_suggestions(self, requirement: TravelRequirement) -> list[str]:
        suggestions = []
        if requirement.companions == "未指定":
            suggestions.append("可以补充同行人群，例如亲子、情侣、老人或朋友。")
        if not requirement.food_preferences:
            suggestions.append("可以补充餐饮偏好或忌口，例如想吃本地菜、清淡、素食。")
        if not requirement.must_visit:
            suggestions.append("可以指定必去景点，系统会优先安排。")
        suggestions.append("删除不喜欢的景点后，可以使用智能补景点或重排当天路线。")
        return suggestions

    def _fallback_plan(
        self,
        requirement: TravelRequirement,
        attractions: List[Attraction],
        weather,
        hotels: List[Hotel],
        variant_id: str = "balanced",
    ) -> TripPlan:
        days: List[DayPlan] = []
        hotel = hotels[0]
        target_per_day = 2 if variant_id == "relaxed" or requirement.low_intensity else 3
        per_day = max(1, min(target_per_day, len(attractions) // requirement.days or target_per_day))
        ordered_attractions = list(attractions)
        if variant_id == "deep_dive":
            ordered_attractions = attractions[1:] + attractions[:1]

        for index in range(requirement.days):
            start = index * per_day
            selected = ordered_attractions[start : start + per_day] or ordered_attractions[:per_day]
            day_hotel = hotels[index % len(hotels)]
            meals = self._meals(requirement.city, requirement.budget_level, requirement.food_preferences, [item.location for item in selected])
            variant_summary = {
                "balanced": "控制步行和换乘强度",
                "relaxed": "减少景点数量并保留休息时间",
                "deep_dive": "强化主题串联和深度体验",
            }.get(variant_id, "控制步行和换乘强度")
            days.append(
                DayPlan(
                    day_index=index + 1,
                    date=requirement.start_date + timedelta(days=index),
                    theme=self._theme(requirement.preferences, index),
                    summary=f"第 {index + 1} 天围绕{self._theme(requirement.preferences, index)}展开，{variant_summary}。",
                    transportation=requirement.transportation or "公共交通 + 步行",
                    hotel=day_hotel,
                    attractions=selected,
                    meals=meals,
                    route_points=[item.location for item in selected],
                    estimated_transport_cost=(45 if variant_id == "relaxed" else 80 if variant_id == "deep_dive" else 60)
                    if requirement.budget_level != "高"
                    else 140,
                )
            )

        budget = BudgetCalculator().calculate(days)
        center = attractions[0].location if attractions else hotel.location
        return TripPlan(
            city=requirement.city,
            days_count=requirement.days,
            preferences=requirement.preferences,
            budget_level=requirement.budget_level,
            days=days,
            weather=weather,
            budget=budget,
            map_center=center,
            overall_suggestions=[
                "热门景点建议提前预约，并把身份证件随身携带。",
                "每天保留 30-60 分钟机动时间，方便根据天气和体力调整。",
                "删除或调整景点顺序后，预算与地图点位会自动同步。",
            ],
            agent_trace=[AttractionSearchAgent.name, WeatherQueryAgent.name, HotelAgent.name, self.name],
        )

    def _theme(self, preferences: List[str], index: int) -> str:
        return preferences[index % len(preferences)] if preferences else "经典必游"

    def _meals(self, city: str, budget_level: str, food_preferences: str = "", route_points=None) -> List[Meal]:
        if self.amap is not None:
            return self.amap.search_meals(city, budget_level, food_preferences=food_preferences, route_points=route_points or [])
        return AmapMCPClient(api_key="").search_meals(city, budget_level, food_preferences=food_preferences, route_points=route_points or [])


class TravelAgentOrchestrator:
    def __init__(self, llm: Any | None = None, disable_llm: bool | None = None, disable_external_api: bool | None = None):
        settings = get_settings()
        disable_llm = settings.disable_llm if disable_llm is None else disable_llm
        disable_external_api = settings.disable_external_api if disable_external_api is None else disable_external_api
        self.parser = TravelRequirementParser()
        self.amap = AmapMCPClient(api_key="" if disable_external_api else None)
        self.unsplash = (
            UnsplashMCPClient(access_key="", pexels_api_key="", pixabay_api_key="", enable_open_sources=False)
            if disable_external_api
            else UnsplashMCPClient()
        )
        configured_llm = None if disable_llm else create_llm()
        agent_llm = llm if llm is not None else configured_llm
        planner_llm = agent_llm
        self.attractions = AttractionSearchAgent(self.amap, self.unsplash, llm=agent_llm)
        self.weather = WeatherQueryAgent(self.amap, llm=agent_llm)
        self.hotels = HotelAgent(self.amap, llm=agent_llm)
        self.planner = PlannerAgent(llm=planner_llm, amap=self.amap, image_provider=self.unsplash)
        self.research = DestinationResearchService()

    def plan(self, request: TripPlanRequest) -> TripPlanningResult:
        requirement = self.parser.parse(request.prompt)
        updates = {}
        if request.start_date is not None:
            updates["start_date"] = request.start_date
        if request.days is not None:
            updates["days"] = request.days
        elif request.start_date is not None and request.end_date is not None:
            updates["days"] = max(1, min((request.end_date - request.start_date).days + 1, 30))
        for field_name in (
            "travel_style",
            "companions",
            "transportation",
            "accommodation",
            "food_preferences",
            "must_visit",
            "avoid_places",
            "low_intensity",
        ):
            value = getattr(request, field_name)
            if value not in (None, [], ""):
                updates[field_name] = value
        if updates:
            requirement = requirement.model_copy(update=updates)
        log_agent_event(self.attractions.name, "input", {"requirement": requirement})
        attractions = self.attractions.run(requirement)
        log_agent_event(self.attractions.name, "output", {"attractions": attractions})

        log_agent_event(self.weather.name, "input", {"requirement": requirement})
        weather = self.weather.run(requirement)
        log_agent_event(self.weather.name, "output", {"weather": weather})

        log_agent_event(self.hotels.name, "input", {"requirement": requirement})
        hotels = self.hotels.run(requirement)
        log_agent_event(self.hotels.name, "output", {"hotels": hotels})

        research_context = self.research.research(requirement.city, requirement.preferences, requirement.days)

        log_agent_event(
            self.planner.name,
            "input",
            {
                "requirement": requirement,
                "attractions": attractions,
                "weather": weather,
                "hotels": hotels,
                "research_context": research_context,
            },
        )
        result = self.planner.run(requirement, attractions, weather, hotels, research_context)
        log_agent_event(self.planner.name, "output", {"result": result})
        return result

    def recalculate(
        self,
        plan: TripPlan,
        operation: str = "recalculate_only",
        research_context: List[ResearchSnippet] | None = None,
        day_index: int | None = None,
    ) -> TripPlan:
        updated_plan = plan
        if operation in {"refill_day", "reorder_day"}:
            updated_plan = self._adjust_plan(updated_plan, operation, day_index)
        updated_days = [
            day.model_copy(update={"route_points": [item.location for item in day.attractions]})
            for day in updated_plan.days
        ]
        return updated_plan.model_copy(update={"days": updated_days, "budget": BudgetCalculator().calculate(updated_days)})

    def _adjust_plan(self, plan: TripPlan, operation: str, day_index: int | None) -> TripPlan:
        target_index = max(1, day_index or 1)
        days = list(plan.days)
        target = next((day for day in days if day.day_index == target_index), days[0] if days else None)
        if target is None:
            return plan
        if operation == "reorder_day":
            reordered = sorted(target.attractions, key=lambda item: (item.location.longitude, item.location.latitude))
            days = [day.model_copy(update={"attractions": reordered}) if day.day_index == target.day_index else day for day in days]
            return plan.model_copy(update={"days": days})

        existing_names = {item.name for day in days for item in day.attractions}
        candidates = self.amap.search_pois(plan.city, plan.preferences, limit=12)
        additions = [item for item in candidates if item.name not in existing_names]
        if additions and len(target.attractions) < 3:
            filled = [*target.attractions, *additions[: 3 - len(target.attractions)]]
            days = [day.model_copy(update={"attractions": filled}) if day.day_index == target.day_index else day for day in days]
        return plan.model_copy(update={"days": days})
