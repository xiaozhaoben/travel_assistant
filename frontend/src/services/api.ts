import axios from 'axios'
import type {
  Budget,
  DayPlan,
  ResearchSnippet,
  ServiceHealth,
  TravelNewsIngestResult,
  TravelQAResponse,
  TripFormData,
  TripPlan,
  TripPlanningResult,
  TripPlanResponse,
} from '@/types'

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || ''

const apiClient = axios.create({
  baseURL: API_BASE_URL,
  timeout: 300000,
  headers: {
    'Content-Type': 'application/json',
  },
})

function formToPrompt(formData: TripFormData): string {
  const preferenceText =
    formData.preferences.length > 0 ? `喜欢${formData.preferences.join('、')}` : '偏好经典路线'
  const extra = formData.free_text_input ? `，额外要求：${formData.free_text_input}` : ''
  const advanced = [
    formData.travel_style ? `旅行节奏${formData.travel_style}` : '',
    formData.companions ? `同行人群${formData.companions}` : '',
    formData.food_preferences ? `餐饮偏好${formData.food_preferences}` : '',
    formData.must_visit ? `必去${formData.must_visit}` : '',
    formData.avoid_places ? `避开${formData.avoid_places}` : '',
    formData.low_intensity ? '希望低强度少走路' : '',
  ]
    .filter(Boolean)
    .join('，')
  const advancedText = advanced ? `，${advanced}` : ''
  return `我想去${formData.city}玩 ${formData.travel_days} 天，${preferenceText}，预算${budgetText(
    formData.accommodation,
  )}，交通方式${formData.transportation}${advancedText}${extra}`
}

function budgetText(accommodation: string): string {
  if (accommodation.includes('豪华')) return '高'
  if (accommodation.includes('经济') || accommodation.includes('民宿')) return '低'
  return '中等'
}

function formatRating(value: unknown): string {
  const rating = Number(value)
  return Number.isFinite(rating) ? rating.toFixed(1) : '4.6'
}

function normalizePlan(raw: any, formData: TripFormData): TripPlan {
  const days: DayPlan[] = raw.days.map((day: any) => ({
    date: day.date,
    day_index: Number(day.day_index) - 1,
    description: day.summary || day.description,
    transportation: day.transportation || formData.transportation,
    accommodation: formData.accommodation,
    hotel: day.hotel
      ? {
          name: day.hotel.name,
          address: day.hotel.address,
          location: day.hotel.location,
          price_range: `约${day.hotel.nightly_price || day.hotel.estimated_cost || 0}元/晚`,
          rating: formatRating(day.hotel.rating ?? 4.6),
          distance: day.hotel.description || '靠近主要游览区',
          type: day.hotel.type || formData.accommodation,
          estimated_cost: day.hotel.nightly_price || day.hotel.estimated_cost || 0,
        }
      : undefined,
    attractions: day.attractions.map((attraction: any) => ({
      name: attraction.name,
      address: attraction.address,
      location: attraction.location,
      visit_duration: attraction.visit_duration_minutes || attraction.visit_duration || 120,
      description: attraction.description,
      category: attraction.category,
      rating: attraction.rating,
      image_url: attraction.image_url,
      ticket_price: attraction.ticket_price || 0,
    })),
    meals: day.meals,
  }))

  return {
    city: raw.city,
    start_date: formData.start_date || days[0]?.date,
    end_date: formData.end_date || days[days.length - 1]?.date,
    generation_mode: raw.generation_mode,
    days,
    weather_info:
      raw.weather_info ||
      raw.weather?.map((weather: any) => ({
        date: weather.date,
        day_weather: weather.day_weather,
        night_weather: weather.night_weather,
        day_temp: weather.day_temp,
        night_temp: weather.night_temp,
        wind_direction: weather.wind_direction || weather.wind?.split(' ')[0] || '东北风',
        wind_power: weather.wind_power || weather.wind?.replace(/^.*?\s/, '') || '1-3级',
      })) ||
      [],
    overall_suggestions: Array.isArray(raw.overall_suggestions)
      ? raw.overall_suggestions.join(' ')
      : raw.overall_suggestions,
    budget: raw.budget,
  }
}

function normalizePlanningResult(raw: any, formData: TripFormData): TripPlanningResult {
  const options = (raw.options || []).map((option: any) => ({
    id: option.id,
    title: option.title,
    style: option.style,
    suitable_for: option.suitable_for,
    highlights: option.highlights || [],
    tradeoffs: option.tradeoffs || [],
    plan: normalizePlan(option.plan, formData),
  }))
  const fallbackPlan = raw.days ? normalizePlan(raw, formData) : options[0]?.plan
  return {
    selected_option_id: raw.selected_option_id || options[0]?.id || 'balanced',
    report_id: raw.report_id || null,
    report_created_at: raw.report_created_at || null,
    report_updated_at: raw.report_updated_at || null,
    options:
      options.length > 0
        ? options
        : [
            {
              id: 'balanced',
              title: '经典均衡方案',
              style: '经典均衡',
              suitable_for: '适合首次到访',
              highlights: [],
              tradeoffs: [],
              plan: fallbackPlan,
            },
          ],
    research_context: raw.research_context || [],
    clarifying_suggestions: raw.clarifying_suggestions || [],
    city: fallbackPlan?.city,
    days: fallbackPlan?.days,
    weather_info: fallbackPlan?.weather_info,
    overall_suggestions: fallbackPlan?.overall_suggestions,
    budget: fallbackPlan?.budget,
  }
}

export async function generateTripPlan(formData: TripFormData): Promise<TripPlanResponse> {
  try {
    const response = await apiClient.post('/api/trip/plan', {
      prompt: formToPrompt(formData),
      start_date: formData.start_date,
      end_date: formData.end_date,
      days: formData.travel_days,
      travel_style: formData.travel_style,
      companions: formData.companions,
      transportation: formData.transportation,
      accommodation: formData.accommodation,
      food_preferences: formData.food_preferences,
      must_visit: splitCsv(formData.must_visit),
      avoid_places: splitCsv(formData.avoid_places),
      low_intensity: formData.low_intensity,
    })
    return {
      success: response.data.success,
      message: response.data.message,
      data: response.data.data ? normalizePlanningResult(response.data.data, formData) : undefined,
    }
  } catch (error: any) {
    throw new Error(error.response?.data?.detail || error.message || '生成旅行计划失败')
  }
}

export async function recalculateTripPlan(
  plan: TripPlan,
  options: {
    operation?: 'recalculate_only' | 'refill_day' | 'reorder_day'
    day_index?: number
    research_context?: ResearchSnippet[]
    report_id?: string | null
  } = {},
): Promise<TripPlan> {
  const payload = denormalizePlan(plan)
  const response = await apiClient.post('/api/trip/recalculate', {
    report_id: options.report_id || undefined,
    plan: payload,
    operation: options.operation || 'recalculate_only',
    day_index: options.day_index,
    research_context: options.research_context || [],
  })
  return normalizePlan(response.data.data, {
    city: plan.city,
    start_date: plan.start_date,
    end_date: plan.end_date,
    travel_days: plan.days.length,
    transportation: plan.days[0]?.transportation || '公共交通',
    accommodation: plan.days[0]?.accommodation || '舒适型酒店',
    preferences: [],
    free_text_input: '',
    travel_style: '经典均衡',
    companions: '',
    food_preferences: '',
    must_visit: '',
    avoid_places: '',
    low_intensity: false,
  })
}

function splitCsv(value: string): string[] {
  return value
    .split(/[，,]/)
    .map((item) => item.trim())
    .filter(Boolean)
}

function denormalizePlan(plan: TripPlan): any {
  return {
    city: plan.city,
    days_count: plan.days.length,
    preferences: [],
    budget_level: '中等',
    generation_mode: plan.generation_mode,
    days: plan.days.map((day) => ({
      day_index: day.day_index + 1,
      date: day.date,
      theme: day.description,
      summary: day.description,
      transportation: day.transportation,
      hotel: day.hotel
        ? {
            id: day.hotel.name,
            name: day.hotel.name,
            address: day.hotel.address,
            location: day.hotel.location || day.attractions[0]?.location,
            type: day.hotel.type,
            rating: Number(day.hotel.rating) || 4.6,
            nightly_price: day.hotel.estimated_cost || 0,
            description: day.hotel.distance,
          }
        : undefined,
      attractions: day.attractions.map((attraction, index) => ({
        id: `${day.day_index}-${index}-${attraction.name}`,
        name: attraction.name,
        category: attraction.category || '景点',
        address: attraction.address,
        location: attraction.location,
        visit_duration_minutes: attraction.visit_duration,
        description: attraction.description,
        ticket_price: attraction.ticket_price || 0,
        image_url: attraction.image_url,
      })),
      meals: day.meals,
      route_points: day.attractions.map((attraction) => attraction.location),
      estimated_transport_cost: Math.round((plan.budget?.total_transportation || 0) / Math.max(plan.days.length, 1)),
    })),
    weather: plan.weather_info.map((weather) => ({
      date: weather.date,
      day_weather: weather.day_weather,
      night_weather: weather.night_weather,
      day_temp: weather.day_temp,
      night_temp: weather.night_temp,
      wind: `${weather.wind_direction} ${weather.wind_power}`,
      suggestion: '适合按当天体力和天气灵活调整。',
    })),
    budget: plan.budget as Budget,
    map_center: plan.days[0]?.attractions[0]?.location || { longitude: 116.397128, latitude: 39.916527 },
    overall_suggestions: [plan.overall_suggestions],
    agent_trace: [],
  }
}

export async function healthCheck(): Promise<ServiceHealth> {
  const response = await apiClient.get('/api/health')
  return response.data
}

export async function askTravelQuestion(question: string, topK = 5): Promise<TravelQAResponse> {
  const response = await apiClient.post('/api/qa/ask', {
    question,
    top_k: topK,
  })
  if (!response.data.success || !response.data.data) {
    throw new Error(response.data.message || '智能问答失败')
  }
  return response.data.data
}

export async function ingestTravelNews(feedUrls: string[] = []): Promise<TravelNewsIngestResult> {
  const response = await apiClient.post('/api/news/ingest', {
    feed_urls: feedUrls,
  })
  if (!response.data.success || !response.data.data) {
    throw new Error(response.data.message || '旅行资讯入库失败')
  }
  return response.data.data
}

export async function getAttractionPhoto(name: string): Promise<string> {
  const response = await apiClient.get('/api/poi/photo', { params: { name } })
  return response.data.data.photo_url
}

export default apiClient
