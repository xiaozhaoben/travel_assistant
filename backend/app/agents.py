from __future__ import annotations

import json
import logging
import re
from datetime import timedelta
from typing import Any, List

try:
    from langchain_core.messages import HumanMessage, SystemMessage
except Exception:  # pragma: no cover - optional until dependencies are installed
    HumanMessage = None
    SystemMessage = None

from .llm_service import create_llm
from .config import get_settings
from .logging_config import log_agent_event
from .models import Attraction, DayPlan, Hotel, Meal, TripPlan, TripPlanRequest, TravelRequirement
from .services import AmapMCPClient, BudgetCalculator, TravelRequirementParser, UnsplashMCPClient

logger = logging.getLogger(__name__)


class AttractionSearchAgent:
    name = "AttractionSearchAgent"

    def __init__(self, amap: AmapMCPClient, unsplash: UnsplashMCPClient):
        self.amap = amap
        self.unsplash = unsplash

    def run(self, requirement: TravelRequirement) -> List[Attraction]:
        attractions = self.amap.search_pois(requirement.city, requirement.preferences, limit=requirement.days * 3)
        return [
            attraction.model_copy(update={"image_url": self.unsplash.image_for(f"{requirement.city} {attraction.name}")})
            for attraction in attractions
        ]


class WeatherQueryAgent:
    name = "WeatherQueryAgent"

    def __init__(self, amap: AmapMCPClient):
        self.amap = amap

    def run(self, requirement: TravelRequirement):
        return self.amap.get_weather(requirement.city, requirement.start_date, requirement.days)


class HotelAgent:
    name = "HotelAgent"

    def __init__(self, amap: AmapMCPClient):
        self.amap = amap

    def run(self, requirement: TravelRequirement) -> List[Hotel]:
        return self.amap.search_hotels(requirement.city, requirement.budget_level, limit=3)


class PlannerAgent:
    name = "PlannerAgent"

    def __init__(self, llm: Any | None = None):
        self.llm = llm

    def run(self, requirement: TravelRequirement, attractions: List[Attraction], weather, hotels: List[Hotel]) -> TripPlan:
        if self.llm is not None:
            llm_plan = self._try_llm_plan(requirement, attractions, weather, hotels)
            if llm_plan is not None:
                return llm_plan
        return self._fallback_plan(requirement, attractions, weather, hotels)

    def _try_llm_plan(
        self,
        requirement: TravelRequirement,
        attractions: List[Attraction],
        weather,
        hotels: List[Hotel],
    ) -> TripPlan | None:
        prompt = self._build_prompt(requirement, attractions, weather, hotels)
        try:
            if SystemMessage is not None and HumanMessage is not None:
                messages = [
                    SystemMessage(content="你是专业行程规划专家，只返回符合要求的 JSON。"),
                    HumanMessage(content=prompt),
                ]
            else:
                messages = prompt
            response = self.llm.invoke(messages)
            content = getattr(response, "content", response)
            data = self._extract_json(str(content))
            data = self._normalize_llm_data(data, requirement, attractions, weather, hotels)
            return TripPlan.model_validate(data).model_copy(update={"generation_mode": "llm"})
        except Exception as exc:
            logger.warning("PlannerAgent LLM planning failed, falling back to local planner: %s", exc)
            return None

    def _build_prompt(self, requirement: TravelRequirement, attractions: List[Attraction], weather, hotels: List[Hotel]) -> str:
        selected_attractions = attractions[: min(len(attractions), 6)]
        selected_hotels = hotels[: min(len(hotels), 2)]
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
        }
        return (
            "请根据资料生成完整旅行计划。每天2-3个景点，包含早午晚餐、酒店、路线点、预算和建议；"
            "只返回可解析 JSON，不要 Markdown，不要额外解释。字段必须匹配："
            "city, days_count, preferences, budget_level, days, weather, budget, map_center, overall_suggestions, agent_trace。\n"
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
        normalized["weather"] = normalized.get("weather") or [item.model_dump(mode="json") for item in weather]

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
            repaired_attractions = self._repair_attractions(raw_attractions, attractions, source_attractions, index)
            meals = self._repair_meals(raw_day.get("meals"), requirement.city)
            normalized_days.append(
                {
                    "day_index": raw_day.get("day_index") or raw_day.get("day_number") or index + 1,
                    "date": raw_day.get("date") or (requirement.start_date + timedelta(days=index)).isoformat(),
                    "theme": raw_day.get("theme") or self._theme(requirement.preferences, index),
                    "summary": raw_day.get("summary")
                    or raw_day.get("description")
                    or raw_day.get("suggestion")
                    or f"第 {index + 1} 天围绕{self._theme(requirement.preferences, index)}展开。",
                    "transportation": raw_day.get("transportation") or raw_day.get("transport") or "公共交通 + 步行",
                    "hotel": day_hotel,
                    "attractions": repaired_attractions,
                    "meals": meals,
                    "route_points": [item["location"] for item in repaired_attractions],
                    "estimated_transport_cost": raw_day.get("estimated_transport_cost") or raw_day.get("transport_cost") or 60,
                }
            )
        normalized["days"] = normalized_days
        normalized["map_center"] = normalized.get("map_center") or normalized_days[0]["route_points"][0]
        budget = normalized.get("budget")
        if not isinstance(budget, dict) or budget.get("total", 0) <= 0:
            budget = self._budget_from_day_dicts(normalized_days)
        normalized["budget"] = budget
        return normalized

    def _repair_hotel(self, raw_hotel, fallback_hotel: Hotel) -> dict:
        hotel = fallback_hotel.model_dump(mode="json")
        if not isinstance(raw_hotel, dict):
            return hotel
        for key in ("id", "name", "address", "location", "type", "rating", "nightly_price", "description"):
            if raw_hotel.get(key) not in (None, ""):
                hotel[key] = raw_hotel[key]
        return hotel

    def _repair_attractions(self, raw_items, attractions: List[Attraction], source_attractions: dict, day_index: int) -> list[dict]:
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
            if source is None and attractions:
                source = attractions[min(day_index * 2 + offset, len(attractions) - 1)]
            if source is None:
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

    def _repair_meals(self, raw_items, city: str) -> list[dict]:
        defaults = self._meals(city, "中等")
        raw_list = self._ensure_list(raw_items, [])
        type_map = {"早餐": "breakfast", "午餐": "lunch", "晚餐": "dinner", "小吃": "snack"}
        meals = []
        for index, default in enumerate(defaults):
            raw = raw_list[index] if index < len(raw_list) and isinstance(raw_list[index], dict) else {}
            meal_type = type_map.get(raw.get("type"), raw.get("type") or default.type)
            suggestion = raw.get("name") or raw.get("suggestion") or default.name
            meals.append(
                {
                    "type": meal_type,
                    "name": suggestion,
                    "address": raw.get("address") or default.address,
                    "estimated_cost": raw.get("estimated_cost") or default.estimated_cost,
                    "description": raw.get("description") or suggestion,
                }
            )
        return meals

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

    def _fallback_plan(self, requirement: TravelRequirement, attractions: List[Attraction], weather, hotels: List[Hotel]) -> TripPlan:
        days: List[DayPlan] = []
        hotel = hotels[0]
        per_day = max(2, min(3, len(attractions) // requirement.days or 2))

        for index in range(requirement.days):
            start = index * per_day
            selected = attractions[start : start + per_day] or attractions[:2]
            day_hotel = hotels[index % len(hotels)]
            meals = self._meals(requirement.city, requirement.budget_level)
            days.append(
                DayPlan(
                    day_index=index + 1,
                    date=requirement.start_date + timedelta(days=index),
                    theme=self._theme(requirement.preferences, index),
                    summary=f"第 {index + 1} 天围绕{self._theme(requirement.preferences, index)}展开，控制步行和换乘强度。",
                    transportation="公共交通 + 步行",
                    hotel=day_hotel,
                    attractions=selected,
                    meals=meals,
                    route_points=[item.location for item in selected],
                    estimated_transport_cost=60 if requirement.budget_level != "高" else 140,
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

    def _meals(self, city: str, budget_level: str) -> List[Meal]:
        base = {"低": 35, "中等": 70, "高": 150}.get(budget_level, 70)
        return [
            Meal(type="breakfast", name=f"{city}胡同早餐", address="酒店周边", estimated_cost=max(20, base - 35), description="豆浆、包子或当地早餐，节省出行时间。"),
            Meal(type="lunch", name=f"{city}特色午餐", address="当日景点附近", estimated_cost=base, description="选择评分稳定、排队可控的本地餐厅。"),
            Meal(type="dinner", name=f"{city}风味晚餐", address="夜游区域附近", estimated_cost=base + 35, description="安排在返程动线附近，避免夜间跨城折返。"),
        ]


class TravelAgentOrchestrator:
    def __init__(self, llm: Any | None = None, disable_llm: bool | None = None, disable_external_api: bool | None = None):
        settings = get_settings()
        disable_llm = settings.disable_llm if disable_llm is None else disable_llm
        disable_external_api = settings.disable_external_api if disable_external_api is None else disable_external_api
        self.parser = TravelRequirementParser()
        self.amap = AmapMCPClient(api_key="" if disable_external_api else None)
        self.unsplash = UnsplashMCPClient(access_key="" if disable_external_api else None)
        self.attractions = AttractionSearchAgent(self.amap, self.unsplash)
        self.weather = WeatherQueryAgent(self.amap)
        self.hotels = HotelAgent(self.amap)
        configured_llm = None if disable_llm else create_llm()
        self.planner = PlannerAgent(llm=llm if llm is not None else configured_llm)

    def plan(self, request: TripPlanRequest) -> TripPlan:
        requirement = self.parser.parse(request.prompt)
        updates = {}
        if request.start_date is not None:
            updates["start_date"] = request.start_date
        if request.days is not None:
            updates["days"] = request.days
        elif request.start_date is not None and request.end_date is not None:
            updates["days"] = max(1, min((request.end_date - request.start_date).days + 1, 30))
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

        log_agent_event(
            self.planner.name,
            "input",
            {
                "requirement": requirement,
                "attractions": attractions,
                "weather": weather,
                "hotels": hotels,
            },
        )
        plan = self.planner.run(requirement, attractions, weather, hotels)
        log_agent_event(self.planner.name, "output", {"plan": plan})
        return plan

    def recalculate(self, plan: TripPlan) -> TripPlan:
        updated_days = [
            day.model_copy(update={"route_points": [item.location for item in day.attractions]})
            for day in plan.days
        ]
        return plan.model_copy(update={"days": updated_days, "budget": BudgetCalculator().calculate(updated_days)})
