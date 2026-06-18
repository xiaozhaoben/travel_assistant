from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from app.integrations.services import RollingGoHotelMCPClient


def create_rollinggo_hotel_search_tool(hotel_client: RollingGoHotelMCPClient):
    @tool(
        "search_hotels",
        description=(
            "通过 RollingGo Hotel MCP 查询真实酒店和实时最低房价。"
            "适合回答住宿推荐、酒店价格、某区域住哪里、预算内酒店筛选等问题。"
            "输入原始问题、目的地或商圈、入住日期、入住晚数、成人数、房间数、儿童数、星级和每晚最高价；"
            "返回酒店名称、地址、坐标、评分、实时最低价、币种、预订链接和标签。"
        ),
    )
    def search_hotels(
        origin_query: str,
        place: str,
        check_in_date: str,
        stay_nights: int = 1,
        adult_count: int = 2,
        room_count: int = 1,
        child_count: int = 0,
        star_level: int | None = None,
        max_price_per_night: int | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        return hotel_client.search_hotels(
            origin_query=origin_query,
            place=place,
            check_in_date=check_in_date,
            stay_nights=stay_nights,
            adult_count=adult_count,
            room_count=room_count,
            child_count=child_count,
            star_level=star_level,
            max_price_per_night=max_price_per_night,
            limit=limit,
        )

    return search_hotels
