export interface Location {
  longitude: number
  latitude: number
}

export interface Attraction {
  name: string
  address: string
  location: Location
  visit_duration: number
  description: string
  category?: string
  rating?: number
  image_url?: string
  ticket_price?: number
}

export interface Meal {
  type: 'breakfast' | 'lunch' | 'dinner' | 'snack'
  name: string
  address?: string
  location?: Location
  description?: string
  estimated_cost?: number
}

export interface Hotel {
  name: string
  address: string
  location?: Location
  price_range: string
  rating: string
  distance: string
  type: string
  estimated_cost?: number
}

export interface Budget {
  total_attractions: number
  total_hotels: number
  total_meals: number
  total_transportation: number
  total: number
}

export interface DayPlan {
  date: string
  day_index: number
  description: string
  transportation: string
  accommodation: string
  hotel?: Hotel
  attractions: Attraction[]
  meals: Meal[]
}

export interface WeatherInfo {
  date: string
  day_weather: string
  night_weather: string
  day_temp: number
  night_temp: number
  wind_direction: string
  wind_power: string
}

export interface TripPlan {
  city: string
  start_date: string
  end_date: string
  generation_mode?: 'llm' | 'fallback'
  days: DayPlan[]
  weather_info: WeatherInfo[]
  overall_suggestions: string
  budget?: Budget
}

export interface ResearchSnippet {
  source: string
  title: string
  url?: string | null
  summary: string
  keywords: string[]
  retrieved_at: string
}

export interface TripPlanOption {
  id: string
  title: string
  style: string
  suitable_for: string
  highlights: string[]
  tradeoffs: string[]
  plan: TripPlan
}

export interface TripPlanningResult {
  selected_option_id: string
  options: TripPlanOption[]
  research_context: ResearchSnippet[]
  clarifying_suggestions: string[]
  city?: string
  days?: DayPlan[]
  weather_info?: WeatherInfo[]
  overall_suggestions?: string
  budget?: Budget
}

export interface TripFormData {
  city: string
  start_date: string
  end_date: string
  travel_days: number
  transportation: string
  accommodation: string
  preferences: string[]
  free_text_input: string
  travel_style: string
  companions: string
  food_preferences: string
  must_visit: string
  avoid_places: string
  low_intensity: boolean
}

export interface TripPlanResponse {
  success: boolean
  message: string
  data?: TripPlanningResult
}

export interface ServiceHealth {
  status: string
  service: string
  llm: {
    enabled: boolean
    model: string
    base_url_configured: boolean
    disabled: boolean
  }
  amap_configured: boolean
  unsplash_configured: boolean
  external_api_disabled: boolean
}
