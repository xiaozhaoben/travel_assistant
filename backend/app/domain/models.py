from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Generic, List, Literal, Optional, TypeVar

from pydantic import BaseModel, Field, computed_field


class TripPlanRequest(BaseModel):
    prompt: str = Field(..., min_length=4, description="Natural language travel request")
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    days: Optional[int] = Field(default=None, ge=1, le=30)
    travel_style: Optional[str] = None
    companions: Optional[str] = None
    transportation: Optional[str] = None
    accommodation: Optional[str] = None
    food_preferences: Optional[str] = None
    must_visit: List[str] = Field(default_factory=list)
    avoid_places: List[str] = Field(default_factory=list)
    low_intensity: bool = False


class TravelRequirement(BaseModel):
    prompt: str
    city: str
    days: int = Field(ge=1, le=30)
    preferences: List[str]
    budget_level: str
    start_date: date
    travel_style: str = "经典均衡"
    companions: str = "未指定"
    transportation: str = "公共交通"
    accommodation: str = "舒适型酒店"
    food_preferences: str = ""
    must_visit: List[str] = Field(default_factory=list)
    avoid_places: List[str] = Field(default_factory=list)
    low_intensity: bool = False


class Location(BaseModel):
    longitude: float
    latitude: float


class Attraction(BaseModel):
    id: str
    name: str
    category: str
    address: str
    location: Location
    visit_duration_minutes: int
    description: str
    ticket_price: int
    image_url: Optional[str] = None
    rating: Optional[float] = None


class Meal(BaseModel):
    type: str
    name: str
    address: str
    estimated_cost: int
    description: str
    id: Optional[str] = None
    location: Optional[Location] = None
    rating: Optional[float] = None
    category: Optional[str] = None


class Hotel(BaseModel):
    id: str
    name: str
    address: str
    location: Location
    type: str
    rating: float
    nightly_price: int
    description: str


class WeatherInfo(BaseModel):
    date: date
    day_weather: str
    night_weather: str
    day_temp: int
    night_temp: int
    wind: str
    suggestion: str


class Budget(BaseModel):
    total_attractions: int = 0
    total_hotels: int = 0
    total_meals: int = 0
    total_transportation: int = 0
    total: int = 0


class DayPlan(BaseModel):
    day_index: int
    date: date
    theme: str
    summary: str
    transportation: str
    hotel: Hotel
    attractions: List[Attraction]
    meals: List[Meal]
    route_points: List[Location]
    estimated_transport_cost: int


class TripPlan(BaseModel):
    city: str
    days_count: int
    preferences: List[str]
    budget_level: str
    generation_mode: Literal["llm", "fallback"] = "fallback"
    days: List[DayPlan]
    weather: List[WeatherInfo]
    budget: Budget
    map_center: Location
    overall_suggestions: List[str]
    agent_trace: List[str]


class ResearchSnippet(BaseModel):
    source: str
    title: str
    url: Optional[str] = None
    summary: str
    keywords: List[str] = Field(default_factory=list)
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TripPlanOption(BaseModel):
    id: str
    title: str
    style: str
    suitable_for: str
    highlights: List[str]
    tradeoffs: List[str]
    plan: TripPlan


class TripPlanningResult(BaseModel):
    selected_option_id: str
    options: List[TripPlanOption]
    research_context: List[ResearchSnippet] = Field(default_factory=list)
    clarifying_suggestions: List[str] = Field(default_factory=list)
    report_id: Optional[str] = None
    report_created_at: Optional[datetime] = None
    report_updated_at: Optional[datetime] = None

    @property
    def selected_plan(self) -> TripPlan:
        for option in self.options:
            if option.id == self.selected_option_id:
                return option.plan
        return self.options[0].plan

    @computed_field
    @property
    def city(self) -> str:
        return self.selected_plan.city

    @computed_field
    @property
    def days_count(self) -> int:
        return self.selected_plan.days_count

    @computed_field
    @property
    def preferences(self) -> List[str]:
        return self.selected_plan.preferences

    @computed_field
    @property
    def budget_level(self) -> str:
        return self.selected_plan.budget_level

    @computed_field
    @property
    def generation_mode(self) -> Literal["llm", "fallback"]:
        return self.selected_plan.generation_mode

    @computed_field
    @property
    def days(self) -> List[DayPlan]:
        return self.selected_plan.days

    @computed_field
    @property
    def weather(self) -> List[WeatherInfo]:
        return self.selected_plan.weather

    @computed_field
    @property
    def budget(self) -> Budget:
        return self.selected_plan.budget

    @computed_field
    @property
    def map_center(self) -> Location:
        return self.selected_plan.map_center

    @computed_field
    @property
    def overall_suggestions(self) -> List[str]:
        return self.selected_plan.overall_suggestions

    @computed_field
    @property
    def agent_trace(self) -> List[str]:
        return self.selected_plan.agent_trace


class PlanEditRequest(BaseModel):
    report_id: Optional[str] = None
    plan: TripPlan
    research_context: List[ResearchSnippet] = Field(default_factory=list)
    operation: Literal["recalculate_only", "refill_day", "reorder_day"] = "recalculate_only"
    day_index: Optional[int] = None


class TripReportRevision(BaseModel):
    id: str
    report_id: str
    operation: str
    plan: dict[str, Any]
    research_context: list[dict[str, Any]] = Field(default_factory=list)
    budget_total: int
    created_at: datetime


class TripReportSummary(BaseModel):
    id: str
    prompt: str
    city: str
    days_count: int
    budget_total: int
    generation_mode: str
    created_at: datetime
    updated_at: datetime


class TripReportDetail(TripReportSummary):
    request: dict[str, Any]
    result: dict[str, Any]
    selected_plan: dict[str, Any]
    revisions: list[TripReportRevision] = Field(default_factory=list)


class TravelKnowledgeSource(BaseModel):
    title: str
    url: Optional[str] = None
    summary: str
    source: str
    published_at: Optional[datetime] = None
    score: float = 0.0


class TravelQARequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=500)
    top_k: int = Field(default=5, ge=1, le=12)


class TravelQAResponse(BaseModel):
    answer: str
    sources: List[TravelKnowledgeSource] = Field(default_factory=list)
    retrieved_count: int = 0
    generation_mode: Literal["llm", "fallback"] = "fallback"


class TravelNewsIngestRequest(BaseModel):
    feed_urls: List[str] = Field(default_factory=list)


class TravelFeedIngestStats(BaseModel):
    url: str
    seen: int = 0
    added: int = 0


class TravelNewsIngestResult(BaseModel):
    total_seen: int = 0
    total_added: int = 0
    feeds: List[TravelFeedIngestStats] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)


T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    success: bool
    message: str
    data: Optional[T] = None
