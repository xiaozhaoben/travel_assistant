from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List

import httpx

from .config import get_settings
from .models import Attraction, Budget, DayPlan, Hotel, Location, TravelRequirement, WeatherInfo

logger = logging.getLogger(__name__)


class TravelRequirementParser:
    """从中文自然语言里提取城市、天数、偏好和预算等级。"""

    city_candidates = ["北京", "上海", "广州", "深圳", "杭州", "成都", "西安", "南京", "苏州", "重庆", "厦门", "青岛"]
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

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        import anyio
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def _call() -> str:
            server = StdioServerParameters(
                command=self.command[0],
                args=self.command[1:],
                env={"AMAP_MAPS_API_KEY": self.api_key},
            )
            async with stdio_client(server) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments)
                    return "\n".join(
                        getattr(item, "text", "")
                        for item in result.content
                        if getattr(item, "text", "")
                    )

        return anyio.run(_call)


class AmapMCPClient:
    """高德地图 MCP 适配器。

    生产环境通过 stdio 启动 `uvx amap-mcp-server` 调用高德地图工具；
    MCP 不可用或没有 API Key 时回退到本地稳定数据，保证开发和测试可运行。
    """

    def __init__(self, api_key: str | None = None, mcp_caller=None):
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.amap_api_key or os.getenv("AMAP_API_KEY") or os.getenv("AMAP_MAPS_API_KEY")
        self.mcp_caller = mcp_caller or (AmapStdioMCPToolCaller(self.api_key) if self.api_key else None)

    def search_pois(self, city: str, keywords: Iterable[str], limit: int = 9) -> List[Attraction]:
        keyword_list = list(keywords)
        if self.mcp_caller:
            pois = self._search_pois_from_mcp(city, keyword_list, limit)
            if pois:
                return pois

        seed = self._beijing_attractions() if city == "北京" else self._generic_attractions(city)
        preferred = [
            item
            for item in seed
            if any(keyword in item.category or keyword in item.description for keyword in keyword_list)
        ]
        ordered = preferred + [item for item in seed if item not in preferred]
        return ordered[:limit]

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

    def _search_pois_from_mcp(self, city: str, keywords: List[str], limit: int) -> List[Attraction]:
        try:
            data = self._call_mcp_json(
                "maps_text_search",
                {"keywords": self._mcp_poi_keywords(keywords), "city": city},
            )
            attractions = []
            for index, poi in enumerate((data.get("pois") or [])[:limit]):
                detail = {}
                poi_id = poi.get("id")
                if poi_id:
                    detail = self._call_mcp_json("maps_search_detail", {"id": poi_id})
                merged = {**poi, **detail}
                location = self._parse_location(merged.get("location"))
                if location:
                    attractions.append(self._poi_to_attraction(merged, index))
            return attractions
        except Exception as exc:
            logger.warning("高德 MCP POI 搜索失败，使用本地景点数据: %s", exc)
            return []

    def _mcp_poi_keywords(self, keywords: List[str]) -> str:
        mapping = {
            "历史文化": "博物馆 古迹 景点",
            "自然风光": "公园 风景名胜",
            "美食": "美食 餐厅 小吃",
            "购物": "商场 步行街",
            "艺术": "美术馆 展览",
            "休闲": "公园 休闲",
            "经典必游": "景点",
        }
        expanded = [mapping.get(keyword, keyword) for keyword in keywords if keyword]
        return " ".join(expanded) or "景点"

    def _search_hotels_from_mcp(self, city: str, budget_level: str, limit: int) -> List[Hotel]:
        try:
            data = self._call_mcp_json(
                "maps_text_search",
                {"keywords": f"{budget_level} 酒店", "city": city},
            )
            price = {"低": 280, "中等": 520, "高": 1100}.get(budget_level, 520)
            hotels = []
            for index, poi in enumerate(data.get("pois", [])[:limit]):
                detail = {}
                poi_id = poi.get("id")
                if poi_id:
                    detail = self._call_mcp_json("maps_search_detail", {"id": poi_id})
                merged = {**poi, **detail}
                location = self._parse_location(merged.get("location"))
                if not location:
                    continue
                rating = self._parse_rating(merged.get("rating")) or 4.6
                hotels.append(
                    Hotel(
                        id=merged.get("id") or f"hotel-{index}",
                        name=merged.get("name") or f"{city}酒店",
                        address=merged.get("address") or f"{city}核心游览区附近",
                        location=location,
                        type=f"{budget_level}型酒店",
                        rating=rating,
                        nightly_price=price + index * 80,
                        description=merged.get("type") or "交通便利，适合多日行程中作为稳定落脚点。",
                    )
                )
            return hotels
        except Exception as exc:
            logger.warning("高德 MCP 酒店搜索失败，使用本地酒店数据: %s", exc)
            return []

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
        result = self.mcp_caller.call_tool(tool_name, arguments)
        if isinstance(result, dict):
            return result
        if isinstance(result, str):
            return self._extract_json_object(result)
        return {}

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

    def _weather_city_code(self, city: str) -> str:
        codes = {
            "北京": "110000",
            "上海": "310000",
            "广州": "440100",
            "深圳": "440300",
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
            "杭州": Location(longitude=120.1551, latitude=30.2741),
            "成都": Location(longitude=104.0665, latitude=30.5723),
            "西安": Location(longitude=108.9398, latitude=34.3416),
        }
        return centers.get(city, Location(longitude=116.397128, latitude=39.916527))

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

    base_url = "https://api.unsplash.com"

    def __init__(self, access_key: str | None = None, http_client=None):
        settings = get_settings()
        self.access_key = access_key if access_key is not None else settings.unsplash_access_key
        self.http_client = http_client or httpx.Client()

    def image_for(self, query: str) -> str:
        if self.access_key:
            url = self._image_from_api(query)
            if url:
                return url
        encoded = query.replace(" ", "%20")
        return f"https://source.unsplash.com/960x640/?{encoded},travel"

    def _image_from_api(self, query: str) -> str | None:
        try:
            response = self.http_client.get(
                f"{self.base_url}/search/photos",
                params={"query": query, "per_page": 1, "orientation": "landscape"},
                headers={"Authorization": f"Client-ID {self.access_key}"},
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()
            first = (data.get("results") or [None])[0]
            if not first:
                return None
            return first.get("urls", {}).get("regular")
        except Exception:
            return None


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
