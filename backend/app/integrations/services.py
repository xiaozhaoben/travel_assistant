from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
from datetime import date, timedelta
from time import perf_counter, sleep
from typing import Any, Dict, Iterable, List
import httpx

from app.core.config import get_settings
from app.domain.models import Attraction, Budget, DayPlan, Hotel, Location, Meal, TravelRequirement, WeatherInfo
from app.integrations.mcp_utils import is_broken_resource_cleanup_error, wait_for_stdio_transport_cleanup
from app.prompts.agent_prompts import AgentPrompts
from app.storage.plan_log import elapsed_ms, record_api_call

logger = logging.getLogger(__name__)

AMAP_QPS_LIMIT_INFO = "CUQPS_HAS_EXCEEDED_THE_LIMIT"
AMAP_QPS_LIMIT_INFOCODE = "10021"
AMAP_HTTP_QPS_RETRY_DELAYS = (1.0, 2.0)


class TravelRequirementParser:
    """从中文自然语言里提取城市、天数、偏好和预算等级。"""

    city_candidates = [
        "北京",
        "上海",
        "广州",
        "深圳",
        "珠海",
        "杭州",
        "成都",
        "西安",
        "南京",
        "苏州",
        "重庆",
        "厦门",
        "青岛",
        "长沙",
        "武汉",
        "天津",
        "大理",
        "丽江",
        "桂林",
        "三亚",
    ]
    preference_keywords: Dict[str, List[str]] = {
        "历史文化": ["历史", "文化", "博物馆", "古迹", "人文"],
        "自然风光": ["自然", "风光", "山水", "公园", "徒步"],
        "美食": ["美食", "小吃", "餐厅", "吃"],
        "亲子": ["亲子", "孩子", "儿童"],
        "艺术": ["艺术", "展览", "画廊"],
        "购物": ["购物", "商场", "买"],
        "休闲": ["休闲", "放松", "慢游"],
    }

    def parse(self, prompt: str) -> TravelRequirement:
        city = self._parse_city(prompt)
        days_match = re.search(r"(\d{1,2})\s*天", prompt)
        days = int(days_match.group(1)) if days_match else 3

        return TravelRequirement(
            prompt=prompt,
            city=city,
            days=max(1, min(days, 30)),
            preferences=self._parse_preferences(prompt) or ["经典必游"],
            budget_level=self._parse_budget(prompt),
            start_date=date.today() + timedelta(days=7),
        )

    def _parse_city(self, prompt: str) -> str:
        for city in self.city_candidates:
            if city in prompt:
                return city
        match = re.search(r"去([\u4e00-\u9fa5]{2,6})(?:玩|旅行|旅游|游)", prompt)
        return match.group(1) if match else "北京"

    def _parse_budget(self, prompt: str) -> str:
        if any(word in prompt for word in ["豪华", "高端", "充足", "预算高"]):
            return "高"
        if any(word in prompt for word in ["经济", "省钱", "低预算", "预算低"]):
            return "低"
        return "中等"

    def _parse_preferences(self, prompt: str) -> List[str]:
        return [
            preference
            for preference, keywords in self.preference_keywords.items()
            if any(keyword in prompt for keyword in keywords)
        ]


class AmapStdioMCPToolCaller:
    """通过 stdio 启动 amap-mcp-server 并调用 MCP 工具。"""

    command = ["uvx", "amap-mcp-server"]

    def __init__(self, api_key: str | None):
        self.api_key = api_key or ""
        self.timeout_seconds = get_settings().mcp_timeout_seconds

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        import anyio
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def _call() -> str:
            command = self._resolve_uvx_command()
            env = {**os.environ, "AMAP_MAPS_API_KEY": self.api_key}
            command_dir = os.path.dirname(command) if os.path.isabs(command) else ""
            if command_dir:
                env["PATH"] = f"{command_dir}{os.pathsep}{env.get('PATH', '')}"
            server = StdioServerParameters(
                command=command,
                args=self.command[1:],
                env=env,
            )
            text: str | None = None
            cleanup_error: BaseException | None = None
            try:
                async with stdio_client(server) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        with anyio.fail_after(self.timeout_seconds):
                            await session.initialize()
                            result = await session.call_tool(tool_name, arguments)
                        text = "\n".join(
                            getattr(item, "text", "")
                            for item in result.content
                            if getattr(item, "text", "")
                        )
            except BaseException as exc:
                if is_broken_resource_cleanup_error(exc):
                    cleanup_error = exc
                else:
                    raise
            await wait_for_stdio_transport_cleanup(anyio)
            if cleanup_error is not None:
                logger.debug("Ignoring MCP stdio cleanup error after successful %s call: %s", tool_name, cleanup_error)
            return text or ""

        return anyio.run(_call)

    def _resolve_uvx_command(self) -> str:
        found = shutil.which(self.command[0])
        if found:
            return found
        local_app_data = os.getenv("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        candidates = [
            os.path.join(local_app_data, "Microsoft", "WinGet", "Links", "uvx.exe"),
            os.path.join(local_app_data, "Programs", "@comfyorgcomfyui-electron", "resources", "uv", "win", "uvx.exe"),
        ]
        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate
        return self.command[0]


class AttractionRecommendationService:
    """Rank raw POI candidates into concrete, travel-worthy attractions."""

    generic_terms = {
        "历史文化景点",
        "历史街区",
        "特色街区",
        "城市公园",
        "城市地标",
        "观景台",
        "美食街",
        "必游景点",
        "风景名胜",
        "旅游路线",
    }
    excluded_terms = {
        "酒店",
        "民宿",
        "公寓",
        "停车",
        "公交",
        "地铁",
        "零食",
        "便利店",
        "餐厅",
        "餐饮",
        "售票",
        "检票",
        "入口",
        "出口",
        "出入口",
        "服务中心",
        "游客中心",
        "讲解",
        "咨询",
        "客服",
        "卫生间",
        "厕所",
        "管理中心",
        "管委会",
        "办公室",
    }
    quality_terms = {
        "风景名胜",
        "旅游景点",
        "博物馆",
        "展览馆",
        "文化",
        "古镇",
        "古村",
        "故居",
        "牌坊",
        "遗址",
        "公园",
        "地标",
        "街区",
        "街",
    }
    preference_terms = {
        "历史文化": {"历史", "文化", "博物", "故居", "古镇", "古村", "遗址", "牌坊", "祠", "街区"},
        "自然风光": {"自然", "风景", "公园", "山", "湖", "海", "岛", "湿地"},
        "美食": {"美食", "小吃", "步行街", "街"},
        "亲子": {"亲子", "儿童", "乐园", "公园", "博物"},
        "艺术": {"艺术", "美术", "展览", "博物"},
        "购物": {"购物", "商业", "步行街"},
        "休闲": {"休闲", "公园", "街区", "海滨"},
        "经典必游": {"地标", "风景名胜", "旅游景点", "博物", "公园"},
    }

    def rank(
        self,
        attractions: List[Attraction],
        city: str,
        preferences: Iterable[str],
        limit: int,
        must_visit: Iterable[str] | None = None,
        avoid_places: Iterable[str] | None = None,
    ) -> List[Attraction]:
        must_visit_list = [str(item).strip() for item in must_visit or [] if str(item).strip()]
        avoid_list = [str(item).strip() for item in avoid_places or [] if str(item).strip()]
        scored: list[tuple[float, Attraction]] = []
        seen: set[str] = set()
        for attraction in attractions:
            key = self._group_key(attraction.name)
            if key in seen:
                continue
            score = self._score(attraction, city, preferences, must_visit_list, avoid_list)
            if score is None:
                continue
            seen.add(key)
            scored.append((score, attraction))

        scored.sort(key=lambda item: item[0], reverse=True)
        return self._select_spatially_diverse([item for _, item in scored], limit)

    def _score(
        self,
        attraction: Attraction,
        city: str,
        preferences: Iterable[str],
        must_visit: list[str],
        avoid_places: list[str],
    ) -> float | None:
        text = f"{attraction.name} {attraction.category} {attraction.address} {attraction.description}"
        if any(place and place in attraction.name for place in avoid_places):
            return None
        if any(term in text for term in self.excluded_terms):
            return None

        score = 0.0
        if attraction.name and attraction.address and attraction.location:
            score += 8
        if city and city in attraction.address:
            score += 3
        if any(term in text for term in self.quality_terms):
            score += 10
        for preference in preferences:
            terms = self.preference_terms.get(str(preference), {str(preference)})
            if any(term and term in text for term in terms):
                score += 12
        if any(place and place in attraction.name for place in must_visit):
            score += 40
        if attraction.rating is not None:
            score += max(0.0, min(float(attraction.rating), 5.0)) * 2
        score -= self._generic_penalty(attraction.name, city)
        if self._looks_like_sub_poi(attraction.name):
            score -= 12
        return score

    def _generic_penalty(self, name: str, city: str) -> float:
        suffix = name.replace(city, "", 1).strip() if city and name.startswith(city) else name.strip()
        if suffix in self.generic_terms:
            return 30
        if any(term == suffix for term in self.generic_terms):
            return 30
        return 0

    def _group_key(self, name: str) -> str:
        return re.sub(r"[\s\-—·路街区景区旅游区]+", "", name)

    def _looks_like_sub_poi(self, name: str) -> bool:
        for separator in ("-", "—", "·"):
            if separator in name:
                parent, child = name.split(separator, 1)
                if len(parent) >= 3 and any(term in child for term in self.excluded_terms):
                    return True
        return False

    def _select_spatially_diverse(self, attractions: List[Attraction], limit: int) -> List[Attraction]:
        selected: list[Attraction] = []
        deferred: list[Attraction] = []
        min_distance_km = 1.2
        for attraction in attractions:
            if not selected or all(self._distance_km(attraction.location, item.location) >= min_distance_km for item in selected):
                selected.append(attraction)
            else:
                deferred.append(attraction)
            if len(selected) >= limit:
                return selected[:limit]
        for attraction in deferred:
            if attraction not in selected:
                selected.append(attraction)
            if len(selected) >= limit:
                break
        return selected[:limit]

    def _distance_km(self, left: Location, right: Location) -> float:
        average_latitude = math.radians((left.latitude + right.latitude) / 2)
        longitude_km = (left.longitude - right.longitude) * 111.0 * math.cos(average_latitude)
        latitude_km = (left.latitude - right.latitude) * 111.0
        return math.hypot(longitude_km, latitude_km)


class AmapMCPClient:
    """高德地图 MCP 适配器。

    生产环境通过 stdio 启动 `uvx amap-mcp-server` 调用高德地图工具；
    MCP 不可用或没有 API Key 时回退到本地稳定数据，保证开发和测试可运行。
    """

    def __init__(
        self,
        api_key: str | None = None,
        mcp_caller=None,
        recommendation_service: AttractionRecommendationService | None = None,
        http_client=None,
    ):
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.amap_api_key or os.getenv("AMAP_API_KEY") or os.getenv("AMAP_MAPS_API_KEY")
        self.mcp_caller = mcp_caller or (AmapStdioMCPToolCaller(self.api_key) if self.api_key else None)
        self.recommendation_service = recommendation_service or AttractionRecommendationService()
        self.http_client = http_client or httpx.Client()

    def close(self) -> None:
        close = getattr(self.http_client, "close", None)
        if callable(close):
            close()

    def search_pois(
        self,
        city: str,
        keywords: Iterable[str],
        limit: int = 9,
        must_visit: Iterable[str] | None = None,
        avoid_places: Iterable[str] | None = None,
        ranking_preferences: Iterable[str] | None = None,
    ) -> List[Attraction]:
        query_list = self._normalize_poi_queries(city, keywords)
        rank_preferences = list(ranking_preferences or query_list)
        if self.mcp_caller:
            pois = self._search_pois_from_mcp(city, query_list, limit)
            if len(pois) >= limit:
                return self.recommendation_service.rank(
                    pois,
                    city=city,
                    preferences=rank_preferences,
                    limit=limit,
                    must_visit=must_visit,
                    avoid_places=avoid_places,
                )
        else:
            pois = []

        seed = self._fallback_attractions_for_city(city)
        preferred = [
            item
            for item in seed
            if any(query.replace(city, "") in item.name or query.replace(city, "") in item.category or query.replace(city, "") in item.description for query in query_list)
        ]
        ranked_pois = self.recommendation_service.rank(
            pois,
            city=city,
            preferences=rank_preferences,
            limit=limit,
            must_visit=must_visit,
            avoid_places=avoid_places,
        )
        fallback_candidates = preferred + [item for item in seed if item not in pois and item not in preferred]
        ranked_fallback = self.recommendation_service.rank(
            fallback_candidates,
            city=city,
            preferences=rank_preferences,
            limit=limit,
            must_visit=must_visit,
            avoid_places=avoid_places,
        )
        combined = ranked_pois + [item for item in ranked_fallback if item.name not in {poi.name for poi in ranked_pois}]
        return combined[:limit]

    def search_hotels(self, city: str, budget_level: str, limit: int = 3) -> List[Hotel]:
        if self.mcp_caller:
            hotels = self._search_hotels_from_mcp(city, budget_level, limit)
            if hotels:
                return hotels

        price = {"低": 280, "中等": 520, "高": 1100}.get(budget_level, 520)
        center = self.city_center(city)
        names = ["城央精选酒店", "慢行精品酒店", "观景舒适酒店"]
        return [
            Hotel(
                id=f"hotel-{index}",
                name=name,
                address=f"{city}核心游览区附近",
                location=Location(longitude=center.longitude + index * 0.012, latitude=center.latitude - index * 0.008),
                type=f"{budget_level}型酒店",
                rating=4.6 - index * 0.1,
                nightly_price=price + index * 80,
                description="交通便利，适合多日行程中作为稳定落脚点。",
            )
            for index, name in enumerate(names)
        ][:limit]

    def search_meals(
        self,
        city: str,
        budget_level: str,
        food_preferences: str = "",
        route_points: Iterable[Location] | None = None,
    ) -> List[Meal]:
        route_point_list = self._normalize_route_points(route_points or [])
        defaults = self._fallback_meals(city, budget_level, route_point_list)
        if self.mcp_caller:
            meals = self._search_meals_from_mcp(city, budget_level, food_preferences, route_point_list)
            if meals:
                by_type = {meal.type: meal for meal in meals}
                return [by_type.get(default.type, default) for default in defaults]
        if self.api_key:
            meals = self._search_meals_from_http(city, budget_level, food_preferences, route_point_list)
            if meals:
                by_type = {meal.type: meal for meal in meals}
                return [by_type.get(default.type, default) for default in defaults]
        return defaults

    def _normalize_route_points(self, route_points: Iterable[Location | dict | str]) -> List[Location]:
        normalized: list[Location] = []
        for point in route_points:
            if isinstance(point, Location):
                normalized.append(point)
                continue
            if isinstance(point, dict):
                longitude = point.get("longitude") or point.get("lng") or point.get("lon")
                latitude = point.get("latitude") or point.get("lat")
                if longitude is not None and latitude is not None:
                    try:
                        normalized.append(Location(longitude=float(longitude), latitude=float(latitude)))
                    except (TypeError, ValueError):
                        pass
                continue
            if isinstance(point, str):
                location = self._parse_location(point)
                if location:
                    normalized.append(location)
        return normalized

    def get_weather(self, city: str, start: date, days: int) -> List[WeatherInfo]:
        if self.mcp_caller:
            weather = self._get_weather_from_mcp(city, start, days)
            if weather:
                return weather

        weathers = [("晴", "多云", 25, 15), ("多云", "晴", 24, 14), ("小雨", "阴", 21, 13), ("晴", "晴", 26, 16)]
        return [
            WeatherInfo(
                date=start + timedelta(days=index),
                day_weather=weathers[index % len(weathers)][0],
                night_weather=weathers[index % len(weathers)][1],
                day_temp=weathers[index % len(weathers)][2],
                night_temp=weathers[index % len(weathers)][3],
                wind="东北风 1-3级",
                suggestion="适合步行游览，雨天建议把室外景点替换为博物馆。",
            )
            for index in range(days)
        ]

    def _search_pois_from_mcp(self, city: str, queries: List[str], limit: int) -> List[Attraction]:
        try:
            attractions = []
            seen: set[str] = set()
            max_query_count = max(1, min(len(queries), 4))
            per_query_limit = max(1, math.ceil(limit / max_query_count))
            for query in queries[:max_query_count]:
                data = self._call_mcp_json("maps_text_search", {"keywords": query, "city": city})
                query_added = 0
                raw_pois = list(data.get("pois") or [])
                raw_pois.sort(key=lambda item: 0 if item.get("name") == query else 1)
                for poi in raw_pois[:limit]:
                    detail = {}
                    poi_id = poi.get("id")
                    if poi_id and not poi.get("location"):
                        detail = self._call_mcp_json("maps_search_detail", {"id": poi_id})
                    merged = {**poi, **detail}
                    unique_key = merged.get("id") or merged.get("name")
                    if not unique_key or unique_key in seen:
                        continue
                    if not self._is_relevant_attraction_poi(merged, city):
                        continue
                    location = self._parse_location(merged.get("location"))
                    if location:
                        seen.add(unique_key)
                        attractions.append(self._poi_to_attraction(merged, len(attractions)))
                        query_added += 1
                    if len(attractions) >= limit:
                        break
                    if query_added >= per_query_limit:
                        break
                if len(attractions) >= limit * 2:
                    break
            return self._select_spatially_diverse_attractions(attractions, limit)
        except Exception as exc:
            logger.warning("高德 MCP POI 搜索失败，使用本地景点数据: %s", exc)
            return []

    def _normalize_poi_queries(self, city: str, keywords: Iterable[str]) -> List[str]:
        unique_queries: list[str] = []
        for keyword in keywords:
            query = str(keyword).strip()
            if not query:
                continue
            broad_terms = {"历史文化", "自然风光", "美食", "购物", "艺术", "休闲", "经典必游", "景点", "风景名胜"}
            if city and city not in query and (query in broad_terms or len(query) <= 4):
                query = f"{city}{query}"
            if query not in unique_queries:
                unique_queries.append(query)
        return unique_queries or [f"{city}景点"]

    def _hotel_queries(self, city: str, budget_level: str) -> List[str]:
        query_map = {
            "低": [f"{city}经济型酒店", f"{city}快捷酒店", f"{city}宾馆"],
            "中等": [f"{city}舒适型酒店", f"{city}酒店", f"{city}商务酒店"],
            "高": [f"{city}高端酒店", f"{city}五星级酒店", f"{city}度假酒店"],
        }
        queries = query_map.get(budget_level, [f"{city}酒店", "酒店"])
        queries = queries + [f"{city}酒店", "酒店"]
        deduped: list[str] = []
        for query in queries:
            if query and query not in deduped:
                deduped.append(query)
        return deduped

    def _search_hotels_from_mcp(self, city: str, budget_level: str, limit: int) -> List[Hotel]:
        try:
            price = {"低": 280, "中等": 520, "高": 1100}.get(budget_level, 520)
            hotels = []
            seen: set[str] = set()
            for query in self._hotel_queries(city, budget_level):
                data = self._call_mcp_json("maps_text_search", {"keywords": query, "city": city})
                for poi in data.get("pois", [])[: max(limit * 3, 5)]:
                    poi_key = poi.get("id") or poi.get("name")
                    if poi_key in seen:
                        continue
                    seen.add(poi_key)
                    detail = {}
                    poi_id = poi.get("id")
                    if poi_id and not poi.get("location"):
                        detail = self._call_mcp_json("maps_search_detail", {"id": poi_id})
                    merged = {**poi, **detail}
                    location = self._parse_location(merged.get("location"))
                    if not location:
                        continue
                    rating = self._parse_rating(merged.get("rating")) or 4.6
                    hotels.append(
                        Hotel(
                            id=merged.get("id") or f"hotel-{len(hotels)}",
                            name=merged.get("name") or f"{city}酒店",
                            address=merged.get("address") or f"{city}核心游览区附近",
                            location=location,
                            type=f"{budget_level}型酒店",
                            rating=rating,
                            nightly_price=price + len(hotels) * 80,
                            description=merged.get("type") or "交通便利，适合多日行程中作为稳定落脚点。",
                        )
                    )
                    if len(hotels) >= limit:
                        return hotels
            return hotels
        except Exception as exc:
            logger.warning("高德 MCP 酒店搜索失败，使用本地酒店数据: %s", exc)
            return []

    def _search_meals_from_mcp(self, city: str, budget_level: str, food_preferences: str, route_points: List[Location]) -> List[Meal]:
        try:
            meals = []
            for meal_type, label in (("breakfast", "早餐"), ("lunch", "午餐"), ("dinner", "晚餐")):
                query = self._meal_query(city, label, food_preferences)
                data = self._call_mcp_json("maps_text_search", {"keywords": query, "city": city})
                candidates = []
                for poi in data.get("pois", [])[:8]:
                    detail = {}
                    poi_id = poi.get("id")
                    if poi_id and not poi.get("location"):
                        detail = self._call_mcp_json("maps_search_detail", {"id": poi_id})
                    merged = {**poi, **detail}
                    if not self._is_relevant_restaurant_poi(merged, city):
                        continue
                    location = self._parse_location(merged.get("location"))
                    if not location:
                        continue
                    candidates.append(self._poi_to_meal(merged, meal_type, budget_level, location, route_points))
                if candidates:
                    candidates.sort(key=lambda item: self._meal_sort_key(item, route_points))
                    meals.append(candidates[0])
            return meals
        except Exception as exc:
            logger.warning("高德 MCP 餐饮搜索失败，使用本地餐饮数据: %s", exc)
            return []

    def _search_meals_from_http(self, city: str, budget_level: str, food_preferences: str, route_points: List[Location]) -> List[Meal]:
        try:
            meals = []
            for meal_type, label in (("breakfast", "\u65e9\u9910"), ("lunch", "\u5348\u9910"), ("dinner", "\u665a\u9910")):
                query = self._meal_query(city, label, food_preferences)
                data = self._amap_place_text(query, city)
                candidates = []
                for poi in data.get("pois", [])[:3]:
                    merged = dict(poi)
                    if not merged.get("city") and merged.get("cityname"):
                        merged["city"] = merged.get("cityname")
                    if not self._is_relevant_restaurant_poi(merged, city):
                        continue
                    location = self._parse_location(merged.get("location"))
                    if not location:
                        continue
                    candidates.append(self._poi_to_meal(merged, meal_type, budget_level, location, route_points))
                if candidates:
                    candidates.sort(key=lambda item: self._meal_sort_key(item, route_points))
                    meals.append(candidates[0])
            return meals
        except Exception as exc:
            logger.warning("高德 HTTP 餐饮搜索失败，使用本地餐饮数据: %s", exc)
            return []

    def _amap_place_text(self, keywords: str, city: str) -> dict:
        start = perf_counter()
        params = {
            "key": self.api_key,
            "keywords": keywords,
            "city": city,
            "citylimit": "true",
            "offset": 10,
            "extensions": "all",
            "output": "json",
        }
        try:
            for attempt, delay in enumerate((0.0, *AMAP_HTTP_QPS_RETRY_DELAYS)):
                if delay:
                    sleep(delay)
                response = self.http_client.get(
                    "https://restapi.amap.com/v3/place/text",
                    params=params,
                    timeout=10,
                )
                response.raise_for_status()
                data = response.json()
                if str(data.get("status")) == "1":
                    record_api_call(
                        component="amap_http",
                        operation="place_text",
                        request_payload={"url": "https://restapi.amap.com/v3/place/text", "params": params},
                        response_payload=data,
                        duration_ms=elapsed_ms(start),
                    )
                    return data
                if not self._is_amap_qps_limit(data) or attempt >= len(AMAP_HTTP_QPS_RETRY_DELAYS):
                    raise RuntimeError(data.get("info") or "Amap place text search failed")
                logger.info(
                    "高德 HTTP QPS 超限，等待 %.1f 秒后重试 place/text: keywords=%s city=%s",
                    AMAP_HTTP_QPS_RETRY_DELAYS[attempt],
                    keywords,
                    city,
                )
            raise RuntimeError("Amap place text search failed")
        except Exception as exc:
            record_api_call(
                component="amap_http",
                operation="place_text",
                request_payload={"url": "https://restapi.amap.com/v3/place/text", "params": params},
                error=str(exc),
                duration_ms=elapsed_ms(start),
            )
            raise

    def _is_amap_qps_limit(self, data: dict) -> bool:
        return str(data.get("infocode")) == AMAP_QPS_LIMIT_INFOCODE or data.get("info") == AMAP_QPS_LIMIT_INFO

    def _get_weather_from_mcp(self, city: str, start: date, days: int) -> List[WeatherInfo]:
        try:
            data = self._call_mcp_json("maps_weather", {"city": self._weather_city_code(city)})
            casts = data.get("forecasts") or []
            result = []
            for index, cast in enumerate(casts[:days]):
                result.append(
                    WeatherInfo(
                        date=start + timedelta(days=index),
                        day_weather=cast.get("dayweather") or "",
                        night_weather=cast.get("nightweather") or "",
                        day_temp=self._parse_int(cast.get("daytemp")),
                        night_temp=self._parse_int(cast.get("nighttemp")),
                        wind=f"{cast.get('daywind') or ''}风 {cast.get('daypower') or ''}级".strip(),
                        suggestion="请结合当天气温和降雨情况调整室内外景点顺序。",
                    )
                )
            return result
        except Exception as exc:
            logger.warning("高德 MCP 天气查询失败，使用本地天气数据: %s", exc)
            return []

    def _call_mcp_json(self, tool_name: str, arguments: dict[str, Any]) -> dict:
        if not self.mcp_caller:
            return {}
        start = perf_counter()
        try:
            result = self.mcp_caller.call_tool(tool_name, arguments)
            if isinstance(result, dict):
                data = result
            elif isinstance(result, str):
                data = self._extract_json_object(result)
            else:
                data = {}
            record_api_call(
                component="amap_mcp",
                operation=tool_name,
                request_payload=arguments,
                response_payload=data,
                duration_ms=elapsed_ms(start),
            )
            return data
        except Exception as exc:
            record_api_call(
                component="amap_mcp",
                operation=tool_name,
                request_payload=arguments,
                error=str(exc),
                duration_ms=elapsed_ms(start),
            )
            raise

    def _extract_json_object(self, text: str) -> dict:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if not match:
                return {}
            return json.loads(match.group(0))

    def _poi_to_attraction(self, poi: dict, index: int) -> Attraction:
        location = self._parse_location(poi.get("location")) or self.city_center("")
        return Attraction(
            id=poi.get("id") or f"poi-{index}",
            name=poi.get("name") or "未命名景点",
            category=poi.get("type") or "景点",
            address=poi.get("address") or "",
            location=location,
            visit_duration_minutes=120,
            description=f"{poi.get('name') or '该景点'}位于{poi.get('address') or '目的地城市'}，适合加入当日游览路线。",
            ticket_price=self._estimate_ticket_price(poi.get("type") or "", index),
            rating=self._parse_rating(poi.get("biz_ext", {}).get("rating")),
        )

    def _poi_to_meal(self, poi: dict, meal_type: str, budget_level: str, location: Location, route_points: List[Location]) -> Meal:
        name = poi.get("name") or "餐饮推荐"
        category = poi.get("type") or poi.get("typecode") or "餐饮"
        address = poi.get("address") or ""
        rating = self._parse_rating(poi.get("biz_ext", {}).get("rating")) or self._parse_rating(poi.get("rating"))
        distance_hint = ""
        if route_points:
            nearest = min(self._distance_km(location, point) for point in route_points)
            distance_hint = f"，距离当日路线约{nearest:.1f}公里"
        return Meal(
            id=poi.get("id"),
            type=meal_type,
            name=name,
            address=address,
            estimated_cost=self._estimate_meal_cost(meal_type, budget_level),
            description=f"{category}{distance_hint}，适合安排为{self._meal_label(meal_type)}。",
            location=location,
            rating=rating,
            category=category,
        )

    def _fallback_meals(self, city: str, budget_level: str, route_points: List[Location] | None = None) -> List[Meal]:
        local_meals = self._local_meals(city, budget_level)
        if local_meals:
            selected = []
            for meal_type in ("breakfast", "lunch", "dinner"):
                candidates = [meal for meal in local_meals if meal.type == meal_type]
                if candidates:
                    candidates.sort(key=lambda item: self._meal_sort_key(item, route_points or []))
                    selected.append(candidates[0])
            if selected:
                return selected
        base = {"低": 35, "中等": 70, "高": 150}.get(budget_level, 70)
        return [
            Meal(type="breakfast", name=f"{city}胡同早餐", address="酒店周边", estimated_cost=max(20, base - 35), description="豆浆、包子或当地早餐，节省出行时间。"),
            Meal(type="lunch", name=f"{city}特色午餐", address="当日景点附近", estimated_cost=base, description="选择评分稳定、排队可控的本地餐厅。"),
            Meal(type="dinner", name=f"{city}风味晚餐", address="夜游区域附近", estimated_cost=base + 35, description="安排在返程动线附近，避免夜间跨城折返。"),
        ]

    def _local_meals(self, city: str, budget_level: str) -> List[Meal]:
        base = {"低": 35, "中等": 70, "高": 150}.get(budget_level, 70)
        if city != "珠海":
            return []
        return [
            Meal(
                id="local-zhuhai-breakfast-xinhaili",
                type="breakfast",
                name="新海利海鲜餐厅",
                address="珠海市香洲区",
                estimated_cost=max(35, base),
                description="珠海老牌早茶和海鲜餐厅，适合安排广式早茶。",
                rating=4.5,
                category="餐饮服务;早茶;海鲜",
            ),
            Meal(
                id="local-zhuhai-lunch-yijian",
                type="lunch",
                name="益健美食大广场",
                address="珠海市香洲区",
                estimated_cost=base,
                description="本地综合型粤菜餐厅，适合午餐和多人同行。",
                rating=4.4,
                category="餐饮服务;中餐厅;粤菜",
            ),
            Meal(
                id="local-zhuhai-dinner-jinyuexuan",
                type="dinner",
                name="金悦轩海鲜酒家",
                address="珠海市香洲区情侣路沿线",
                estimated_cost=max(base + 35, 120),
                description="以海鲜和广式点心为特色，适合晚餐或景观用餐。",
                rating=4.6,
                category="餐饮服务;中餐厅;海鲜",
            ),
        ]

    def _meal_query(self, city: str, label: str, food_preferences: str) -> str:
        preference = food_preferences.strip()
        if preference:
            return f"{city}{preference}{label}"
        return f"{city}{label}"

    def _meal_label(self, meal_type: str) -> str:
        return {"breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐"}.get(meal_type, "餐饮")

    def _estimate_meal_cost(self, meal_type: str, budget_level: str) -> int:
        base = {"低": 35, "中等": 70, "高": 150}.get(budget_level, 70)
        if meal_type == "breakfast":
            return max(20, base - 35)
        if meal_type == "dinner":
            return base + 35
        return base

    def _meal_sort_key(self, meal: Meal, route_points: List[Location]) -> tuple[float, float]:
        rating_score = -(meal.rating or 0)
        if route_points and meal.location:
            distance = min(self._distance_km(meal.location, point) for point in route_points)
            return (distance, rating_score)
        return (0.0, rating_score)

    def _estimate_ticket_price(self, category: str, index: int) -> int:
        if any(keyword in category for keyword in ["博物馆", "科教文化"]):
            return 30
        if any(keyword in category for keyword in ["风景名胜", "世界遗产", "公园"]):
            return 50 + (index % 3) * 10
        if any(keyword in category for keyword in ["宗教", "寺庙", "纪念馆"]):
            return 20
        return 30 + (index % 2) * 10

    def _parse_location(self, value: str | None) -> Location | None:
        if not value or "," not in value:
            return None
        longitude, latitude = value.split(",", 1)
        return Location(longitude=float(longitude), latitude=float(latitude))

    def _parse_rating(self, value) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _parse_int(self, value) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    def _is_relevant_attraction_poi(self, poi: dict, city: str) -> bool:
        poi_city = str(poi.get("city") or "")
        if poi_city and city not in poi_city:
            return False
        name = str(poi.get("name") or "")
        category = str(poi.get("type") or poi.get("typecode") or "")
        address = str(poi.get("address") or "")
        searchable_text = f"{name} {category} {address}"
        excluded_terms = [
            "酒店",
            "民宿",
            "公寓",
            "停车",
            "公交",
            "地铁",
            "零食",
            "便利店",
            "餐厅",
            "餐饮",
            "售票",
            "检票",
            "入口",
            "出口",
            "出入口",
            "服务中心",
            "游客中心",
            "观众服务",
            "讲解",
            "咨询",
            "客服",
            "卫生间",
            "厕所",
            "暂停开放",
            "管理中心",
            "管委会",
            "办公室",
        ]
        if any(term in searchable_text for term in excluded_terms):
            return False
        if self._looks_like_sub_poi(name):
            return False
        allowed_terms = ["风景名胜", "旅游景点", "博物馆", "展览馆", "文化", "古镇", "古村", "故居", "牌坊", "公园", "街"]
        return any(term in searchable_text for term in allowed_terms)

    def _is_relevant_restaurant_poi(self, poi: dict, city: str) -> bool:
        poi_city = str(poi.get("city") or "")
        if poi_city and city not in poi_city:
            return False
        name = str(poi.get("name") or "")
        category = str(poi.get("type") or poi.get("typecode") or "")
        address = str(poi.get("address") or "")
        searchable_text = f"{name} {category} {address}"
        restaurant_terms = ["餐饮", "餐厅", "中餐", "西餐", "小吃", "咖啡", "茶", "快餐", "火锅", "海鲜", "素食", "早茶", "面馆", "饭店"]
        excluded_terms = ["便利店", "零食", "超市", "商店", "酒店", "宾馆", "停车场", "公交", "地铁"]
        if any(term in searchable_text for term in excluded_terms):
            return False
        return any(term in searchable_text for term in restaurant_terms)

    def _unique_attractions(self, attractions: List[Attraction]) -> List[Attraction]:
        unique: list[Attraction] = []
        seen_keys: set[str] = set()
        for attraction in attractions:
            key = self._attraction_group_key(attraction.name)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            unique.append(attraction)
        return unique

    def _select_spatially_diverse_attractions(self, attractions: List[Attraction], limit: int) -> List[Attraction]:
        unique = self._unique_attractions(attractions)
        selected: list[Attraction] = []
        deferred: list[Attraction] = []
        min_distance_km = 1.2
        for attraction in unique:
            if not selected or all(self._distance_km(attraction.location, item.location) >= min_distance_km for item in selected):
                selected.append(attraction)
            else:
                deferred.append(attraction)
            if len(selected) >= limit:
                return selected[:limit]
        for attraction in deferred:
            if attraction not in selected:
                selected.append(attraction)
            if len(selected) >= limit:
                break
        return selected[:limit]

    def _distance_km(self, left: Location, right: Location) -> float:
        average_latitude = math.radians((left.latitude + right.latitude) / 2)
        longitude_km = (left.longitude - right.longitude) * 111.0 * math.cos(average_latitude)
        latitude_km = (left.latitude - right.latitude) * 111.0
        return math.hypot(longitude_km, latitude_km)

    def _looks_like_sub_poi(self, name: str) -> bool:
        for separator in ("-", "—", "·"):
            if separator not in name:
                continue
            parent, child = name.split(separator, 1)
            parent_indicators = ["博物院", "博物馆", "公园", "景区", "旅游区", "风景区", "遗址公园", "故宫"]
            valid_child_indicators = ["故居", "古村", "古镇", "岛", "街区"]
            if any(term in parent for term in parent_indicators) and not any(term in child for term in valid_child_indicators):
                return True
        return False

    def _attraction_group_key(self, name: str) -> str:
        clean_name = re.sub(r"[（(].*?[）)]", "", name).strip()
        for separator in ("-", "—"):
            if separator in clean_name:
                parent = clean_name.split(separator, 1)[0].strip()
                if len(parent) >= 3:
                    return parent
        return clean_name

    def _weather_city_code(self, city: str) -> str:
        codes = {
            "北京": "110000",
            "上海": "310000",
            "广州": "440100",
            "深圳": "440300",
            "珠海": "440400",
            "杭州": "330100",
            "成都": "510100",
            "西安": "610100",
            "南京": "320100",
            "苏州": "320500",
            "重庆": "500000",
            "厦门": "350200",
            "青岛": "370200",
        }
        return codes.get(city, city)

    def city_center(self, city: str) -> Location:
        centers = {
            "北京": Location(longitude=116.397128, latitude=39.916527),
            "上海": Location(longitude=121.4737, latitude=31.2304),
            "广州": Location(longitude=113.2644, latitude=23.1291),
            "深圳": Location(longitude=114.0579, latitude=22.5431),
            "珠海": Location(longitude=113.5767, latitude=22.2707),
            "杭州": Location(longitude=120.1551, latitude=30.2741),
            "成都": Location(longitude=104.0665, latitude=30.5723),
            "西安": Location(longitude=108.9398, latitude=34.3416),
            "南京": Location(longitude=118.7969, latitude=32.0603),
            "苏州": Location(longitude=120.5853, latitude=31.2989),
            "重庆": Location(longitude=106.5516, latitude=29.5630),
            "厦门": Location(longitude=118.0894, latitude=24.4798),
            "青岛": Location(longitude=120.3826, latitude=36.0671),
            "长沙": Location(longitude=112.9388, latitude=28.2282),
            "武汉": Location(longitude=114.3054, latitude=30.5931),
            "天津": Location(longitude=117.2000, latitude=39.1333),
            "大理": Location(longitude=100.2676, latitude=25.6065),
            "丽江": Location(longitude=100.2330, latitude=26.8721),
            "桂林": Location(longitude=110.2900, latitude=25.2736),
            "三亚": Location(longitude=109.5119, latitude=18.2528),
        }
        return centers.get(city, Location(longitude=116.397128, latitude=39.916527))

    def _fallback_attractions_for_city(self, city: str) -> List[Attraction]:
        if city == "北京":
            return self._beijing_attractions()
        if city == "珠海":
            return self._zhuhai_attractions()
        if city == "广州":
            return self._guangzhou_attractions()
        return self._generic_attractions(city)

    def _beijing_attractions(self) -> List[Attraction]:
        data = [
            ("poi-1", "故宫博物院", "历史文化", "北京市东城区景山前街4号", 116.397, 39.916, 240, "明清皇家宫殿建筑群，适合深入理解北京历史轴线。", 60),
            ("poi-2", "天坛公园", "历史文化", "北京市东城区天坛东里甲1号", 116.410, 39.882, 150, "古代祭天建筑群，空间开阔，早晨游览体验最好。", 34),
            ("poi-3", "颐和园", "历史文化", "北京市海淀区新建宫门路19号", 116.275, 39.999, 210, "皇家园林代表，昆明湖和万寿山适合慢游。", 30),
            ("poi-4", "国家博物馆", "历史文化", "北京市东城区东长安街16号", 116.401, 39.905, 180, "系统了解中国历史文化脉络，雨天尤其适合。", 0),
            ("poi-5", "八达岭长城", "历史文化", "北京市延庆区G6京藏高速58号出口", 116.016, 40.356, 240, "长城经典段落，建议预留半天并早出发。", 40),
            ("poi-6", "什刹海", "休闲美食", "北京市西城区地安门西大街", 116.386, 39.940, 120, "胡同、水岸和老北京餐饮集中，适合傍晚散步。", 0),
            ("poi-7", "雍和宫", "历史文化", "北京市东城区雍和宫大街12号", 116.417, 39.947, 90, "藏传佛教寺院建筑精美，可与国子监街串联。", 25),
            ("poi-8", "南锣鼓巷", "美食购物", "北京市东城区南锣鼓巷", 116.404, 39.937, 90, "胡同商业街，适合小吃和伴手礼。", 0),
            ("poi-9", "景山公园", "自然风光", "北京市西城区景山西街44号", 116.395, 39.925, 75, "登高俯瞰故宫中轴线，适合作为故宫后续行程。", 2),
        ]
        return [
            Attraction(
                id=item[0],
                name=item[1],
                category=item[2],
                address=item[3],
                location=Location(longitude=item[4], latitude=item[5]),
                visit_duration_minutes=item[6],
                description=item[7],
                ticket_price=item[8],
            )
            for item in data
        ]

    def _zhuhai_attractions(self) -> List[Attraction]:
        data = [
            (
                "zhuhai-1",
                "珠海博物馆",
                "历史文化",
                "珠海市香洲区海虹路88号",
                113.576561,
                22.292980,
                120,
                "展示珠海城市发展、海洋文化与近现代历史，适合作为珠海文化线的起点。",
                0,
            ),
            (
                "zhuhai-2",
                "唐家古镇",
                "历史文化",
                "珠海市香洲区唐家湾镇",
                113.593346,
                22.359661,
                150,
                "保留岭南古镇街巷与名人故居，可串联唐家湾历史人文街区慢游。",
                0,
            ),
            (
                "zhuhai-3",
                "梅溪牌坊旅游区",
                "历史文化",
                "珠海市香洲区前山旅游路268号",
                113.515988,
                22.285039,
                150,
                "以陈芳家族历史、岭南建筑和牌坊群为核心，适合了解珠海侨乡文化。",
                65,
            ),
            (
                "zhuhai-4",
                "圆明新园",
                "历史文化",
                "珠海市香洲区兰埔路与白石路交界处",
                113.539606,
                22.242634,
                180,
                "以清代园林建筑复原与演艺体验为特色，适合半日休闲文化游。",
                0,
            ),
            (
                "zhuhai-5",
                "淇澳岛-苏兆征故居",
                "历史文化",
                "珠海市香洲区唐家镇淇澳村白石街461号",
                113.646670,
                22.412428,
                120,
                "红色文化与淇澳古村街巷结合，适合安排为北部人文支线。",
                0,
            ),
            (
                "zhuhai-6",
                "会同古村旅游区",
                "历史文化",
                "珠海市香洲区会同北路附近",
                113.516595,
                22.353639,
                120,
                "保存较完整的岭南村落格局，适合摄影、古村漫步和轻量文化体验。",
                0,
            ),
            (
                "zhuhai-7",
                "珠海渔女",
                "经典必游",
                "珠海市香洲区情侣中路香炉湾",
                113.588277,
                22.261417,
                60,
                "珠海城市地标，适合与情侣路、海滨公园串联安排。",
                0,
            ),
            (
                "zhuhai-8",
                "白石街",
                "历史文化",
                "珠海市香洲区唐家湾镇淇澳岛",
                113.644437,
                22.410680,
                90,
                "淇澳岛历史街巷，适合与苏兆征故居一起作为岛上文化路线。",
                0,
            ),
            (
                "zhuhai-9",
                "共乐园",
                "历史文化",
                "珠海市香洲区唐家湾镇山房路",
                113.590114,
                22.362921,
                90,
                "唐家湾近代园林遗存，可与唐家古镇组合成北部历史文化半日线。",
                0,
            ),
            (
                "zhuhai-10",
                "杨氏大宗祠",
                "历史文化",
                "珠海市香洲区南屏镇北山村",
                113.515456,
                22.221310,
                60,
                "岭南宗祠建筑代表，适合与南屏、北山街区串联体验本地历史。",
                0,
            ),
            (
                "zhuhai-11",
                "拉塔石炮台",
                "历史文化",
                "珠海市香洲区湾仔街道",
                113.546498,
                22.225924,
                60,
                "近代海防遗址，适合补充珠海口岸与海防历史背景。",
                0,
            ),
        ]
        return [
            Attraction(
                id=item[0],
                name=item[1],
                category=item[2],
                address=item[3],
                location=Location(longitude=item[4], latitude=item[5]),
                visit_duration_minutes=item[6],
                description=item[7],
                ticket_price=item[8],
            )
            for item in data
        ]

    def _guangzhou_attractions(self) -> List[Attraction]:
        data = [
            ("guangzhou-1", "陈家祠", "历史文化", "广州市荔湾区中山七路恩龙里34号", 113.2466, 23.1317, 120, "岭南祠堂建筑代表，木雕、砖雕、陶塑保存精美，适合作为广州历史文化线起点。", 10),
            ("guangzhou-2", "沙面岛", "历史文化", "广州市荔湾区沙面北街", 113.2384, 23.1092, 120, "近代租界建筑群与珠江岸线街区，适合步行、摄影和了解广州近代史。", 0),
            ("guangzhou-3", "越秀公园", "历史文化", "广州市越秀区解放北路988号", 113.2640, 23.1482, 150, "广州老城核心公园，五羊石像、镇海楼和古城墙可串联游览。", 0),
            ("guangzhou-4", "南越王博物院", "历史文化", "广州市越秀区解放北路867号", 113.2621, 23.1434, 120, "展示南越国宫署、王墓和岭南早期文明，适合深度了解广州城市源流。", 10),
            ("guangzhou-5", "广州塔", "经典必游", "广州市海珠区阅江西路222号", 113.3307, 23.1066, 120, "广州城市地标，可俯瞰珠江新城和珠江两岸夜景。", 150),
            ("guangzhou-6", "广东省博物馆", "历史文化", "广州市天河区珠江东路2号", 113.3235, 23.1194, 150, "综合展示岭南历史、自然与艺术，适合雨天或亲子文化行程。", 0),
            ("guangzhou-7", "北京路步行街", "美食购物", "广州市越秀区北京路", 113.2694, 23.1244, 90, "广州传统商业街区，保留千年古道遗址，适合晚间餐饮和散步。", 0),
            ("guangzhou-8", "永庆坊", "历史文化", "广州市荔湾区恩宁路99号", 113.2438, 23.1144, 120, "西关骑楼、粤剧艺术和老城更新街区，适合体验广州本地生活。", 0),
            ("guangzhou-9", "白云山风景名胜区", "自然风光", "广州市白云区广园中路801号", 113.2978, 23.1820, 180, "广州代表性山体景区，适合半日轻徒步和城市远眺。", 5),
        ]
        return [
            Attraction(
                id=item[0],
                name=item[1],
                category=item[2],
                address=item[3],
                location=Location(longitude=item[4], latitude=item[5]),
                visit_duration_minutes=item[6],
                description=item[7],
                ticket_price=item[8],
            )
            for item in data
        ]

    def _generic_attractions(self, city: str) -> List[Attraction]:
        center = self.city_center(city)
        names = ["博物馆", "历史街区", "城市公园", "特色街区", "观景地", "美食街"]
        return [
            Attraction(
                id=f"poi-{index}",
                name=f"{city}{name}",
                category="经典必游",
                address=f"{city}市中心区域",
                location=Location(longitude=center.longitude + index * 0.01, latitude=center.latitude + index * 0.008),
                visit_duration_minutes=120,
                description=f"{city}代表性目的地，适合首次到访安排。",
                ticket_price=30 + index * 10,
            )
            for index, name in enumerate(names)
        ]


class UnsplashMCPClient:
    """Unsplash MCP 风格图片适配器。"""

    unsplash_base_url = "https://api.unsplash.com"
    pexels_base_url = "https://api.pexels.com/v1"
    pixabay_base_url = "https://pixabay.com/api/"
    openverse_base_url = "https://api.openverse.org/v1"
    wikimedia_base_url = "https://commons.wikimedia.org/w/api.php"

    def __init__(
        self,
        access_key: str | None = None,
        pexels_api_key: str | None = None,
        pixabay_api_key: str | None = None,
        enable_open_sources: bool = True,
        http_client=None,
        web_search_client=None,
        llm: Any | None = None,
        enable_llm_selector: bool = True,
    ):
        settings = get_settings()
        self.access_key = access_key if access_key is not None else settings.unsplash_access_key
        self.pexels_api_key = pexels_api_key if pexels_api_key is not None else settings.pexels_api_key
        self.pixabay_api_key = pixabay_api_key if pixabay_api_key is not None else settings.pixabay_api_key
        self.wikimedia_user_agent = settings.wikimedia_user_agent
        self.enable_open_sources = enable_open_sources
        self.http_client = http_client or httpx.Client()
        self.web_search_client = web_search_client if web_search_client is not None else self._create_web_search_client()
        self.llm = llm if llm is not None else (
            self._create_llm()
            if enable_llm_selector and self.web_search_client is not None and getattr(self.web_search_client, "available", False)
            else None
        )

    def close(self) -> None:
        close = getattr(self.http_client, "close", None)
        if callable(close):
            close()

    def image_for(self, query: str, use_api: bool = True) -> str:
        if use_api:
            for provider, search in self._provider_order():
                self._log_image_event("image_search_attempt", query, provider=provider)
                url = search(query)
                if url:
                    self._log_image_event("image_search_success", query, provider=provider, url=url)
                    return url
                self._log_image_event("image_search_no_result", query, provider=provider)
        self._log_image_event("image_search_fallback", query, url="")
        return ""

    def _provider_order(self):
        providers = []
        if self.web_search_client is not None and getattr(self.web_search_client, "available", False):
            providers.append(("web_search", self._image_from_web_search))
        if self.enable_open_sources:
            providers.extend([
                ("wikimedia", self._image_from_wikimedia),
                ("openverse", self._image_from_openverse),
            ])
        if self.pexels_api_key:
            providers.append(("pexels", self._image_from_pexels))
        if self.pixabay_api_key:
            providers.append(("pixabay", self._image_from_pixabay))
        if self.access_key:
            providers.append(("unsplash", self._image_from_unsplash))
        return providers

    def _create_web_search_client(self):
        if not self.enable_open_sources:
            return None
        try:
            from app.researching.research import WebSearchMCPClient

            return WebSearchMCPClient()
        except Exception:
            return None

    def _create_llm(self):
        try:
            if get_settings().disable_llm:
                return None
            from app.core.llm_service import create_llm

            return create_llm()
        except Exception:
            return None

    def _image_from_web_search(self, query: str) -> str | None:
        if self.web_search_client is None:
            return None
        search_query = f"{query} landmark photo image"
        try:
            candidates = []
            for item in self._search_image_candidates(search_query)[:5]:
                url = self._extract_image_url(item)
                if url:
                    candidates.append(
                        {
                            "title": str(item.get("title") or item.get("name") or "")[:120],
                            "page_url": item.get("url") or item.get("link"),
                            "image_url": url,
                            "summary": str(item.get("content") or item.get("summary") or item.get("snippet") or "")[:220],
                        }
                    )
            if not candidates:
                return None
            selected = self._choose_image_with_llm(query, candidates)
            if selected:
                return selected
            if self.llm is None:
                return candidates[0]["image_url"]
        except Exception as exc:
            self._log_image_event("image_search_error", query, provider="web_search", error=str(exc))
            return None
        return None

    def _search_image_candidates(self, query: str) -> list[dict[str, Any]]:
        extra_args: dict[str, Any] | None = None
        if getattr(self.web_search_client, "tool_name", "") == "tavily_search":
            extra_args = {
                "include_images": True,
                "include_image_descriptions": True,
                "max_results": 5,
            }
        try:
            return self.web_search_client.search(query, extra_args=extra_args)
        except TypeError:
            return self.web_search_client.search(query)

    def _choose_image_with_llm(self, query: str, candidates: list[dict[str, Any]]) -> str | None:
        if self.llm is None:
            return None
        allowed_urls = {str(candidate.get("image_url")) for candidate in candidates if candidate.get("image_url")}
        prompt = AgentPrompts.render_image_selection(query, candidates)
        try:
            response = self.llm.invoke([("user", prompt)])
            content = getattr(response, "content", response)
            data = self._parse_llm_json(str(content))
            selected = str(data.get("image_url") or data.get("url") or "").strip()
            if selected in allowed_urls:
                return selected
        except Exception as exc:
            self._log_image_event("image_search_error", query, provider="web_search_llm", error=str(exc))
        return None

    def _parse_llm_json(self, content: str) -> dict[str, Any]:
        try:
            data = json.loads(content)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", content, re.DOTALL)
            if not match:
                return {}
            try:
                data = json.loads(match.group(0))
                return data if isinstance(data, dict) else {}
            except json.JSONDecodeError:
                return {}

    def _extract_image_url(self, item: Any) -> str | None:
        if not isinstance(item, dict):
            return None
        direct_fields = (
            "image",
            "image_url",
            "imageUrl",
            "thumbnail",
            "thumbnail_url",
            "thumbnailUrl",
            "contentUrl",
            "content_url",
        )
        for field in direct_fields:
            candidate = item.get(field)
            if isinstance(candidate, str) and self._is_http_url(candidate):
                return candidate
            if isinstance(candidate, dict):
                nested = self._extract_image_url(candidate)
                if nested:
                    return nested
        for container_field in ("images", "image_urls", "thumbnails"):
            container = item.get(container_field)
            if isinstance(container, list):
                for candidate in container:
                    if isinstance(candidate, str) and self._is_http_url(candidate):
                        return candidate
                    if isinstance(candidate, dict):
                        nested = self._extract_image_url(candidate)
                        if nested:
                            return nested
        text = " ".join(str(item.get(field) or "") for field in ("content", "summary", "snippet"))
        images_section = text.split("Images:", 1)[1] if "Images:" in text else ""
        for candidate in re.findall(r"URL:\s*(https?://[^\s\"'<>]+)", images_section):
            if self._is_http_url(candidate):
                return candidate.rstrip(").,;]")
        for candidate in re.findall(r"https?://[^\s\"'<>]+", text):
            if self._looks_like_image_url(candidate):
                return candidate.rstrip(").,;]")
        return None

    def _is_http_url(self, url: str) -> bool:
        return url.strip().lower().startswith(("http://", "https://"))

    def _looks_like_image_url(self, url: str) -> bool:
        lower = url.strip().lower()
        if not self._is_http_url(url):
            return False
        return bool(re.search(r"\.(?:jpg|jpeg|png|webp|gif)(?:[?#].*)?$", lower))

    def _image_from_wikimedia(self, query: str) -> str | None:
        try:
            data = self._get_json(
                self.wikimedia_base_url,
                params={
                    "action": "query",
                    "format": "json",
                    "generator": "search",
                    "gsrsearch": query,
                    "gsrnamespace": 6,
                    "gsrlimit": 1,
                    "prop": "imageinfo",
                    "iiprop": "url",
                    "iiurlwidth": 960,
                },
                headers={
                    "User-Agent": self.wikimedia_user_agent,
                    "Api-User-Agent": self.wikimedia_user_agent,
                },
            )
            pages_payload = (data.get("query") or {}).get("pages") or {}
            pages = list(pages_payload.values()) if isinstance(pages_payload, dict) else pages_payload
            first = pages[0] if pages else None
            if isinstance(first, dict):
                image_info = (first.get("imageinfo") or [None])[0]
                if isinstance(image_info, dict):
                    return image_info.get("thumburl") or image_info.get("url")
        except Exception as exc:
            self._log_image_event("image_search_error", query, provider="wikimedia", error=str(exc))
            return None
        return None

    def _image_from_openverse(self, query: str) -> str | None:
        try:
            data = self._get_json(
                f"{self.openverse_base_url}/images/",
                params={"q": query, "page_size": 1},
            )
            first = (data.get("results") or [None])[0]
            if isinstance(first, dict):
                return first.get("thumbnail") or first.get("url")
        except Exception as exc:
            self._log_image_event("image_search_error", query, provider="openverse", error=str(exc))
            return None
        return None

    def _image_from_pexels(self, query: str) -> str | None:
        try:
            data = self._get_json(
                f"{self.pexels_base_url}/search",
                params={"query": query, "per_page": 1, "orientation": "landscape"},
                headers={"Authorization": self.pexels_api_key},
            )
            first = (data.get("photos") or [None])[0]
            if isinstance(first, dict):
                src = first.get("src") or {}
                return src.get("large") or src.get("medium") or src.get("original")
        except Exception as exc:
            self._log_image_event("image_search_error", query, provider="pexels", error=str(exc))
            return None
        return None

    def _image_from_pixabay(self, query: str) -> str | None:
        try:
            data = self._get_json(
                self.pixabay_base_url,
                params={
                    "key": self.pixabay_api_key,
                    "q": query,
                    "image_type": "photo",
                    "orientation": "horizontal",
                    "per_page": 3,
                },
            )
            first = (data.get("hits") or [None])[0]
            if isinstance(first, dict):
                return first.get("largeImageURL") or first.get("webformatURL")
        except Exception as exc:
            self._log_image_event("image_search_error", query, provider="pixabay", error=str(exc))
            return None
        return None

    def _image_from_unsplash(self, query: str) -> str | None:
        try:
            data = self._get_json(
                f"{self.unsplash_base_url}/search/photos",
                params={"query": query, "per_page": 1, "orientation": "landscape"},
                headers={"Authorization": f"Client-ID {self.access_key}"},
            )
            first = (data.get("results") or [None])[0]
            if not first:
                return None
            return first.get("urls", {}).get("regular")
        except Exception as exc:
            self._log_image_event("image_search_error", query, provider="unsplash", error=str(exc))
            return None

    def _get_json(self, url: str, params: dict, headers: dict | None = None) -> dict:
        start = perf_counter()
        try:
            response = self.http_client.get(url, params=params, headers=headers, timeout=10)
            response.raise_for_status()
            data = response.json()
            record_api_call(
                component="image_http",
                operation="get_json",
                request_payload={"url": url, "params": params, "headers": headers or {}},
                response_payload=data,
                duration_ms=elapsed_ms(start),
            )
            return data
        except Exception as exc:
            record_api_call(
                component="image_http",
                operation="get_json",
                request_payload={"url": url, "params": params, "headers": headers or {}},
                error=str(exc),
                duration_ms=elapsed_ms(start),
            )
            raise

    def _log_image_event(
        self,
        event: str,
        query: str,
        provider: str | None = None,
        url: str | None = None,
        error: str | None = None,
    ) -> None:
        payload = {
            "event": event,
            "query": query,
        }
        if provider:
            payload["provider"] = provider
        if url:
            payload["url"] = url
        if error:
            payload["error"] = error[:300]
        logger.info(json.dumps(payload, ensure_ascii=False))


class BudgetCalculator:
    def calculate(self, days: List[DayPlan]) -> Budget:
        total_attractions = sum(attraction.ticket_price for day in days for attraction in day.attractions)
        total_hotels = sum(day.hotel.nightly_price for day in days)
        total_meals = sum(meal.estimated_cost for day in days for meal in day.meals)
        total_transportation = sum(day.estimated_transport_cost for day in days)
        total = total_attractions + total_hotels + total_meals + total_transportation
        return Budget(
            total_attractions=total_attractions,
            total_hotels=total_hotels,
            total_meals=total_meals,
            total_transportation=total_transportation,
            total=total,
        )
