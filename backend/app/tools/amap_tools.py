from __future__ import annotations

import json
from datetime import date
from typing import Any

from langchain_core.tools import tool

from app.domain.models import Location
from app.integrations.services import AmapMCPClient


def _parse_list_param(value: Any) -> list:
    """Safely parse a value that may be a list, a JSON string, or None.

    LangChain Agent sometimes passes list parameters as JSON strings
    instead of actual Python lists, causing tool validation failures.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []
    return []


def create_attraction_search_tool(amap: AmapMCPClient):
    @tool(
        "search_attractions",
        description=(
            "通过高德地图 MCP 搜索真实景点 POI。适合在规划景点前调用。"
            "输入城市、具体 POI 搜索关键词列表、返回数量、用户必去/避开地点和偏好；"
            "返回可直接用于行程规划的景点 JSON 列表。"
        ),
    )
    def search_attractions(
        city: str,
        keywords: list[str],
        limit: int = 9,
        must_visit: list[str] | None = None,
        avoid_places: list[str] | None = None,
        ranking_preferences: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        attractions = amap.search_pois(
            city=city,
            keywords=_parse_list_param(keywords),
            limit=limit,
            must_visit=_parse_list_param(must_visit) or None,
            avoid_places=_parse_list_param(avoid_places) or None,
            ranking_preferences=_parse_list_param(ranking_preferences) or None,
        )
        return [item.model_dump(mode="json") for item in attractions]

    return search_attractions


def create_weather_query_tool(amap: AmapMCPClient):
    @tool(
        "query_weather",
        description=(
            "通过高德地图 MCP 查询旅行城市未来天气。适合在安排行程强度、室内外景点顺序前调用。"
            "输入城市、开始日期和天数；返回每日白天/夜间天气、温度、风力和出行建议。"
        ),
    )
    def query_weather(city: str, start_date: str, days: int) -> list[dict[str, Any]]:
        weather = amap.get_weather(city=city, start=date.fromisoformat(start_date), days=days)
        return [item.model_dump(mode="json") for item in weather]

    return query_weather


def create_hotel_search_tool(amap: AmapMCPClient):
    @tool(
        "search_hotels",
        description=(
            "通过高德地图 MCP 搜索城市酒店 POI。适合在确定住宿方案时调用。"
            "输入城市、预算等级和返回数量；返回酒店名称、地址、坐标、评分、价格和描述。"
        ),
    )
    def search_hotels(city: str, budget_level: str, limit: int = 3) -> list[dict[str, Any]]:
        hotels = amap.search_hotels(city=city, budget_level=budget_level, limit=limit)
        return [item.model_dump(mode="json") for item in hotels]

    return search_hotels


def create_meal_search_tool(amap: AmapMCPClient):
    @tool(
        "search_meals",
        description=(
            "通过高德地图 MCP 按每日路线搜索附近餐饮。适合行程规划 agent 为每天补充早餐、午餐、晚餐时调用。"
            "输入城市、预算等级、饮食偏好和路线坐标点；返回餐厅或餐饮建议 JSON 列表。"
        ),
    )
    def search_meals(
        city: str,
        budget_level: str,
        food_preferences: str = "",
        route_points: list[dict[str, float]] | None = None,
    ) -> list[dict[str, Any]]:
        parsed_route = _parse_list_param(route_points)
        locations = [
            Location(longitude=float(item["longitude"]), latitude=float(item["latitude"]))
            for item in parsed_route
            if isinstance(item, dict) and "longitude" in item and "latitude" in item
        ]
        meals = amap.search_meals(
            city=city,
            budget_level=budget_level,
            food_preferences=food_preferences,
            route_points=locations,
        )
        return [item.model_dump(mode="json") for item in meals]

    return search_meals
