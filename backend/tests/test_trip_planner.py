import json
import logging
from datetime import date, timedelta

from fastapi.testclient import TestClient

from app.agents import PlannerAgent, TravelAgentOrchestrator
from app.config import get_settings
from app.main import app
import app.main as main_module
from app.models import TripPlanRequest
from app.services import AmapMCPClient, BudgetCalculator, TravelRequirementParser, UnsplashMCPClient


class FakeMessage:
    def __init__(self, content: str):
        self.content = content


class FakeLLM:
    def __init__(self, content: str):
        self.content = content
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        return FakeMessage(self.content)


class FakeHttpResponse:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self.data


class FakeHttpClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        return FakeHttpResponse(self.responses.pop(0))


class FakeMCPCaller:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def call_tool(self, tool_name, arguments):
        self.calls.append({"tool_name": tool_name, "arguments": arguments})
        return self.responses.pop(0)


def test_settings_support_reference_env_names(monkeypatch):
    monkeypatch.setenv("LLM_MODEL_ID", "qwen3.6-plus")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    monkeypatch.setenv("LLM_TIMEOUT", "45")
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:5173,http://localhost:5174")
    monkeypatch.setenv("AMAP_API_KEY", "amap-key")

    settings = get_settings()

    assert settings.llm_model_id == "qwen3.6-plus"
    assert settings.llm_api_key == "test-key"
    assert settings.llm_base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert settings.llm_timeout == 45
    assert settings.cors_origins == ["http://localhost:5173", "http://localhost:5174"]
    assert settings.amap_api_key == "amap-key"
    assert settings.has_llm_credentials is True


def test_settings_support_provider_disable_flags(monkeypatch):
    monkeypatch.setenv("DISABLE_LLM", "true")
    monkeypatch.setenv("DISABLE_EXTERNAL_API", "1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("AMAP_API_KEY", "amap-key")
    monkeypatch.setenv("UNSPLASH_ACCESS_KEY", "unsplash-key")

    settings = get_settings()
    orchestrator = TravelAgentOrchestrator()

    assert settings.disable_llm is True
    assert settings.disable_external_api is True
    assert orchestrator.planner.llm is None
    assert orchestrator.amap.api_key == ""
    assert orchestrator.unsplash.access_key == ""


def test_llm_settings_support_dashscope_thinking_toggle(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_ENABLE_THINKING", "false")

    settings = get_settings()

    assert settings.llm_enable_thinking is False


def test_create_llm_passes_dashscope_thinking_toggle(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL_ID", "qwen3.6-plus")
    monkeypatch.setenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    monkeypatch.setenv("LLM_ENABLE_THINKING", "false")

    from app.llm_service import create_llm

    llm = create_llm()

    assert llm is not None
    assert llm.extra_body == {"enable_thinking": False}


def test_parser_extracts_city_days_preferences_and_budget():
    parser = TravelRequirementParser()

    requirement = parser.parse("我想去北京玩 3 天，喜欢历史文化，预算中等")

    assert requirement.city == "北京"
    assert requirement.days == 3
    assert requirement.preferences == ["历史文化"]
    assert requirement.budget_level == "中等"


def test_amap_client_uses_mcp_for_poi_search_and_detail():
    caller = FakeMCPCaller(
        [
            {
                "pois": [
                    {
                        "id": "B000A",
                        "name": "测试景点",
                        "address": "测试地址",
                        "typecode": "110200",
                    }
                ],
            },
            {
                "id": "B000A",
                "name": "测试景点",
                "type": "风景名胜",
                "address": "测试地址",
                "location": "116.40,39.90",
                "rating": "4.8",
            },
        ]
    )
    amap = AmapMCPClient(api_key="amap-key", mcp_caller=caller)

    pois = amap.search_pois("北京", ["历史文化"], limit=1)

    assert caller.calls[0] == {
        "tool_name": "maps_text_search",
        "arguments": {"keywords": "博物馆 古迹 景点", "city": "北京"},
    }
    assert caller.calls[1] == {
        "tool_name": "maps_search_detail",
        "arguments": {"id": "B000A"},
    }
    assert pois[0].name == "测试景点"
    assert pois[0].location.longitude == 116.40
    assert pois[0].ticket_price > 0


def test_amap_client_uses_mcp_for_weather():
    caller = FakeMCPCaller(
        [
            {
                "city": "北京市",
                "forecasts": [
                    {
                        "date": "2026-05-20",
                        "dayweather": "晴",
                        "nightweather": "多云",
                        "daytemp": "28",
                        "nighttemp": "18",
                        "daywind": "东北",
                        "daypower": "1-3",
                    }
                ],
            }
        ]
    )
    amap = AmapMCPClient(api_key="amap-key", mcp_caller=caller)

    weather = amap.get_weather("北京", date(2026, 5, 20), 1)

    assert caller.calls[0] == {
        "tool_name": "maps_weather",
        "arguments": {"city": "110000"},
    }
    assert weather[0].day_weather == "晴"
    assert weather[0].day_temp == 28


def test_unsplash_client_uses_configured_api_for_images():
    client = FakeHttpClient(
        [
            {
                "results": [
                    {
                        "urls": {
                            "regular": "https://images.example/photo.jpg",
                        }
                    }
                ]
            }
        ]
    )
    unsplash = UnsplashMCPClient(access_key="unsplash-key", http_client=client)

    url = unsplash.image_for("北京 故宫")

    assert client.calls[0]["url"].endswith("/search/photos")
    assert client.calls[0]["headers"]["Authorization"] == "Client-ID unsplash-key"
    assert url == "https://images.example/photo.jpg"


def test_orchestrator_generates_complete_plan_with_four_agent_outputs():
    orchestrator = TravelAgentOrchestrator(disable_llm=True, disable_external_api=True)
    request = TripPlanRequest(prompt="我想去北京玩 3 天，喜欢历史文化，预算中等")

    plan = orchestrator.plan(request)

    assert plan.city == "北京"
    assert len(plan.days) == 3
    assert len(plan.weather) == 3
    assert plan.budget.total > 0
    assert plan.agent_trace == [
        "AttractionSearchAgent",
        "WeatherQueryAgent",
        "HotelAgent",
        "PlannerAgent",
    ]
    for day in plan.days:
        assert len(day.attractions) >= 2
        assert len(day.meals) == 3
        assert day.hotel.name


def test_orchestrator_logs_each_agent_input_and_output_with_timestamp(caplog):
    orchestrator = TravelAgentOrchestrator(disable_llm=True, disable_external_api=True)

    with caplog.at_level(logging.INFO, logger="travel_assistant.agent"):
        orchestrator.plan(TripPlanRequest(prompt="我想去北京玩 1 天，喜欢历史文化，预算中等"))

    events = [json.loads(record.message) for record in caplog.records]
    expected_agents = [
        "AttractionSearchAgent",
        "WeatherQueryAgent",
        "HotelAgent",
        "PlannerAgent",
    ]

    for agent_name in expected_agents:
        agent_events = [event for event in events if event["agent"] == agent_name]
        assert [event["event"] for event in agent_events] == ["input", "output"]
        assert all(event["timestamp"] for event in agent_events)
        assert all("payload" in event for event in agent_events)


def test_planner_agent_invokes_llm_when_model_is_available():
    fake_plan = _fake_llm_plan()
    llm = FakeLLM(json.dumps(fake_plan, ensure_ascii=False))
    orchestrator = TravelAgentOrchestrator(llm=llm)

    plan = orchestrator.plan(TripPlanRequest(prompt="我想去北京玩 1 天，喜欢历史文化，预算中等"))

    assert llm.calls, "PlannerAgent should call the injected LangChain-compatible LLM"
    assert plan.city == "北京"
    assert plan.days[0].summary == "LLM生成的历史文化一日游"
    assert plan.budget.total == 860
    assert plan.generation_mode == "llm"


def test_planner_agent_marks_fallback_generation_mode_without_model():
    orchestrator = TravelAgentOrchestrator(disable_llm=True, disable_external_api=True)

    plan = orchestrator.plan(TripPlanRequest(prompt="我想去北京玩 1 天，喜欢历史文化，预算中等"))

    assert plan.generation_mode == "fallback"


def test_planner_agent_repairs_common_llm_schema_variants():
    start = date.today() + timedelta(days=7)
    llm_payload = {
        "city": "北京",
        "days_count": 1,
        "preferences": ["历史文化"],
        "budget_level": "中等",
        "days": [
            {
                "day_number": 1,
                "date": start.isoformat(),
                "theme": "历史文化",
                "transport": "地铁 + 步行",
                "hotel": {"id": "hotel-0", "name": "模型精选酒店"},
                "activities": [
                    {
                        "name": "故宫博物院",
                        "duration_minutes": 180,
                        "description": "上午参观皇家宫殿建筑群。",
                    }
                ],
                "meals": [
                    {"type": "早餐", "suggestion": "酒店附近胡同早餐", "estimated_cost": 35},
                    {"type": "午餐", "suggestion": "故宫附近京味午餐", "estimated_cost": 75},
                    {"type": "晚餐", "suggestion": "前门烤鸭", "estimated_cost": 120},
                ],
            }
        ],
        "budget": {
            "total_attractions": 0,
            "total_hotels": 0,
            "total_meals": 0,
            "total_transportation": 0,
            "total": 0,
        },
        "overall_suggestions": "热门景点提前预约。",
        "agent_trace": "plan_generated_v1.0",
    }
    llm = FakeLLM(json.dumps(llm_payload, ensure_ascii=False))
    orchestrator = TravelAgentOrchestrator(llm=llm, disable_external_api=True)

    plan = orchestrator.plan(TripPlanRequest(prompt="我想去北京玩 1 天，喜欢历史文化，预算中等"))

    assert plan.generation_mode == "llm"
    assert plan.days[0].day_index == 1
    assert plan.days[0].hotel.name
    assert plan.days[0].attractions[0].address
    assert plan.days[0].meals[0].type == "breakfast"
    assert plan.budget.total > 0
    assert plan.overall_suggestions == ["热门景点提前预约。"]


def test_planner_prompt_limits_source_lists_for_faster_llm_response():
    orchestrator = TravelAgentOrchestrator(disable_llm=True, disable_external_api=True)
    requirement = orchestrator.parser.parse("我想去北京玩 3 天，喜欢历史文化，预算中等")
    attractions = orchestrator.attractions.run(requirement)
    weather = orchestrator.weather.run(requirement)
    hotels = orchestrator.hotels.run(requirement)

    prompt = PlannerAgent()._build_prompt(requirement, attractions, weather, hotels)
    payload = json.loads(prompt.rsplit("\n", 1)[1])

    assert len(payload["attractions"]) <= 6
    assert len(payload["hotels"]) <= 2
    assert len(prompt) < 5000


def test_budget_recalculates_after_deleting_attraction():
    orchestrator = TravelAgentOrchestrator(disable_llm=True, disable_external_api=True)
    plan = orchestrator.plan(TripPlanRequest(prompt="我想去北京玩 2 天，喜欢历史文化，预算中等"))
    removed_price = plan.days[0].attractions[0].ticket_price
    plan.days[0].attractions.pop(0)

    recalculated = BudgetCalculator().calculate(plan.days)

    assert recalculated.total_attractions == plan.budget.total_attractions - removed_price
    assert recalculated.total == (
        recalculated.total_attractions
        + recalculated.total_hotels
        + recalculated.total_meals
        + recalculated.total_transportation
    )


def test_api_health_plan_and_recalculate_endpoints():
    main_module.orchestrator = TravelAgentOrchestrator(disable_llm=True, disable_external_api=True)
    client = TestClient(app)

    health = client.get("/api/health")
    assert health.status_code == 200
    assert "llm" in health.json()
    assert health.json()["amap_transport"] == "mcp-stdio"

    response = client.post("/api/trip/plan", json={"prompt": "我想去北京玩 3 天，喜欢历史文化，预算中等"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["data"]["city"] == "北京"

    plan = payload["data"]
    plan["days"][0]["attractions"] = plan["days"][0]["attractions"][1:]
    recalc = client.post("/api/trip/recalculate", json={"plan": plan})

    assert recalc.status_code == 200
    assert recalc.json()["data"]["budget"]["total"] < plan["budget"]["total"]

    poi = client.get("/api/map/poi", params={"keywords": "历史文化", "city": "北京"})
    assert poi.status_code == 200
    assert poi.json()["success"] is True
    assert poi.json()["data"]

    weather = client.get("/api/map/weather", params={"city": "北京", "days": 2})
    assert weather.status_code == 200
    assert len(weather.json()["data"]) == 2


def test_api_plan_honors_structured_dates_and_days():
    main_module.orchestrator = TravelAgentOrchestrator(disable_llm=True, disable_external_api=True)
    client = TestClient(app)

    response = client.post(
        "/api/trip/plan",
        json={
            "prompt": "我想去北京玩，喜欢历史文化，预算中等",
            "start_date": "2026-06-01",
            "days": 2,
        },
    )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["days_count"] == 2
    assert [day["date"] for day in payload["days"]] == ["2026-06-01", "2026-06-02"]
    assert [item["date"] for item in payload["weather"]] == ["2026-06-01", "2026-06-02"]


def _fake_llm_plan():
    start = date.today() + timedelta(days=7)
    return {
        "city": "北京",
        "days_count": 1,
        "preferences": ["历史文化"],
        "budget_level": "中等",
        "days": [
            {
                "day_index": 1,
                "date": start.isoformat(),
                "theme": "历史文化",
                "summary": "LLM生成的历史文化一日游",
                "transportation": "公共交通 + 步行",
                "hotel": {
                    "id": "hotel-llm",
                    "name": "LLM精选酒店",
                    "address": "北京市东城区",
                    "location": {"longitude": 116.397128, "latitude": 39.916527},
                    "type": "中等型酒店",
                    "rating": 4.7,
                    "nightly_price": 520,
                    "description": "靠近核心景点",
                },
                "attractions": [
                    {
                        "id": "poi-llm-1",
                        "name": "故宫博物院",
                        "category": "历史文化",
                        "address": "北京市东城区景山前街4号",
                        "location": {"longitude": 116.397, "latitude": 39.916},
                        "visit_duration_minutes": 240,
                        "description": "皇家宫殿建筑群",
                        "ticket_price": 60,
                        "image_url": None,
                    }
                ],
                "meals": [
                    {"type": "breakfast", "name": "胡同早餐", "address": "酒店附近", "estimated_cost": 35, "description": "本地早餐"},
                    {"type": "lunch", "name": "京味午餐", "address": "故宫附近", "estimated_cost": 70, "description": "北京菜"},
                    {"type": "dinner", "name": "烤鸭晚餐", "address": "前门附近", "estimated_cost": 120, "description": "经典晚餐"},
                ],
                "route_points": [{"longitude": 116.397, "latitude": 39.916}],
                "estimated_transport_cost": 55,
            }
        ],
        "weather": [
            {
                "date": start.isoformat(),
                "day_weather": "晴",
                "night_weather": "多云",
                "day_temp": 25,
                "night_temp": 15,
                "wind": "东北风 1-3级",
                "suggestion": "适合步行",
            }
        ],
        "budget": {
            "total_attractions": 60,
            "total_hotels": 520,
            "total_meals": 225,
            "total_transportation": 55,
            "total": 860,
        },
        "map_center": {"longitude": 116.397, "latitude": 39.916},
        "overall_suggestions": ["提前预约热门景点"],
        "agent_trace": ["AttractionSearchAgent", "WeatherQueryAgent", "HotelAgent", "PlannerAgent"],
    }
