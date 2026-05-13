from __future__ import annotations

from datetime import date
from typing import Generic, List, Literal, Optional, TypeVar

from pydantic import BaseModel, Field


class TripPlanRequest(BaseModel):
    prompt: str = Field(..., min_length=4, description="Natural language travel request")
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    days: Optional[int] = Field(default=None, ge=1, le=30)


class TravelRequirement(BaseModel):
    prompt: str
    city: str
    days: int = Field(ge=1, le=30)
    preferences: List[str]
    budget_level: str
    start_date: date


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


class Meal(BaseModel):
    type: str
    name: str
    address: str
    estimated_cost: int
    description: str


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


class PlanEditRequest(BaseModel):
    plan: TripPlan


T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    success: bool
    message: str
    data: Optional[T] = None
