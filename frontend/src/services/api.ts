import axios from 'axios'
import { z } from 'zod'
import type {
  AuthTokenResponse,
  AuthUser,
  Budget,
  DayPlan,
  ResearchSnippet,
  ServiceHealth,
  TravelDocumentAutoIngestPayload,
  TravelDocumentIngestJobResponse,
  TravelDocumentIngestJobStatus,
  TravelDocumentIngestPayload,
  TravelDocumentIngestResult,
  TravelDocumentSearchPayload,
  TravelDocumentSearchResponse,
  TravelDocumentUrlIngestPayload,
  TravelNewsIngestResult,
  TravelQAConversationDetail,
  TravelQAConversationSummary,
  TravelQAResponse,
  TripFormData,
  TripPlan,
  TripPlanningResult,
  TripPlanResponse,
  TripReportDetail,
  TripReportSummary,
} from '@/types'

const _rtConfig = (window as any).__APP_CONFIG__ || {}
const API_BASE_URL = _rtConfig.API_BASE_URL || import.meta.env.VITE_API_BASE_URL || ''
const DEFAULT_API_TIMEOUT_MS = 300000
const API_TIMEOUT_MS = Number(_rtConfig.API_TIMEOUT_MS || import.meta.env.VITE_API_TIMEOUT_MS || DEFAULT_API_TIMEOUT_MS)
const TRIP_PLAN_TIMEOUT_MESSAGE = '行程生成耗时较长，请稍后在历史报表获取。'

const apiClient = axios.create({
  baseURL: API_BASE_URL,
  timeout: Number.isFinite(API_TIMEOUT_MS) && API_TIMEOUT_MS > 0 ? API_TIMEOUT_MS : DEFAULT_API_TIMEOUT_MS,
  headers: {
    'Content-Type': 'application/json',
  },
})

function apiUrl(path: string): string {
  if (!API_BASE_URL) return path
  return `${API_BASE_URL.replace(/\/$/, '')}${path}`
}

const TOKEN_KEY = 'travel_auth_token'

apiClient.interceptors.request.use((config) => {
  const token = localStorage.getItem(TOKEN_KEY)
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      localStorage.removeItem(TOKEN_KEY)
      localStorage.removeItem('travel_auth_user')
    }
    return Promise.reject(error)
  },
)

const apiEnvelopeSchema = z
  .object({
    success: z.boolean(),
    message: z.string().default(''),
    data: z.unknown().optional(),
  })
  .passthrough()

function parseApiEnvelope(responseData: unknown, fallbackMessage: string) {
  const parsed = apiEnvelopeSchema.safeParse(responseData)
  if (!parsed.success) {
    throw new Error(fallbackMessage)
  }
  return parsed.data
}

function requireApiData<T>(responseData: unknown, fallbackMessage: string): T {
  const envelope = parseApiEnvelope(responseData, fallbackMessage)
  if (!envelope.success || envelope.data === undefined || envelope.data === null) {
    throw new Error(envelope.message || fallbackMessage)
  }
  return envelope.data as T
}

function errorMessage(error: unknown, fallbackMessage: string): string {
  if (axios.isAxiosError(error)) {
    const detail = (error.response?.data as { detail?: unknown } | undefined)?.detail
    if (typeof detail === 'string' && detail.trim()) return detail
    if (error.message) return error.message
  }
  if (error instanceof Error && error.message) return error.message
  return fallbackMessage
}

function isTimeoutError(error: unknown): boolean {
  if (!axios.isAxiosError(error)) return false
  return (
    error.code === 'ECONNABORTED' ||
    error.code === 'ETIMEDOUT' ||
    error.response?.status === 408 ||
    error.response?.status === 504 ||
    /timeout/i.test(error.message)
  )
}

function tripPlanErrorMessage(error: unknown): string {
  if (isTimeoutError(error)) return TRIP_PLAN_TIMEOUT_MESSAGE
  return errorMessage(error, 'Trip plan generation failed')
}

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
  const days: DayPlan[] = raw.days.map((day: any, index: number) => ({
    date: day.date,
    day_index: Number(day.day_index || index + 1),
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
    quality_report: raw.quality_report,
    city: fallbackPlan?.city,
    days: fallbackPlan?.days,
    weather_info: fallbackPlan?.weather_info,
    overall_suggestions: fallbackPlan?.overall_suggestions,
    budget: fallbackPlan?.budget,
  }
}

function formDataFromReport(raw: any, selectedPlan: any): TripFormData {
  const days = selectedPlan?.days || []
  return {
    city: selectedPlan?.city || raw?.city || '',
    start_date: raw?.start_date || days[0]?.date || '',
    end_date: raw?.end_date || days[days.length - 1]?.date || '',
    travel_days: Number(raw?.days || selectedPlan?.days_count || days.length || 1),
    transportation: raw?.transportation || days[0]?.transportation || '公共交通',
    accommodation: raw?.accommodation || days[0]?.hotel?.type || '舒适型酒店',
    preferences: Array.isArray(selectedPlan?.preferences) ? selectedPlan.preferences : [],
    free_text_input: '',
    travel_style: String(raw?.travel_style || '经典均衡'),
    companions: String(raw?.companions || ''),
    food_preferences: String(raw?.food_preferences || ''),
    must_visit: Array.isArray(raw?.must_visit) ? raw.must_visit.join('，') : '',
    avoid_places: Array.isArray(raw?.avoid_places) ? raw.avoid_places.join('，') : '',
    low_intensity: Boolean(raw?.low_intensity),
  }
}

export function normalizeReportPlanningResult(detail: TripReportDetail): TripPlanningResult {
  const rawResult = {
    ...detail.result,
    report_id: detail.id,
    report_created_at: detail.created_at,
    report_updated_at: detail.updated_at,
  }
  const selectedPlan = detail.selected_plan || (detail.result as any).options?.[0]?.plan
  return normalizePlanningResult(rawResult, formDataFromReport(detail.request, selectedPlan))
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
    const envelope = parseApiEnvelope(response.data, 'Trip plan generation failed')
    return {
      success: envelope.success,
      message: envelope.message,
      data: envelope.data ? normalizePlanningResult(envelope.data, formData) : undefined,
    }
  } catch (error: unknown) {
    throw new Error(tripPlanErrorMessage(error))
  }
}

export async function listTripReports(limit = 50): Promise<TripReportSummary[]> {
  const response = await apiClient.get('/api/reports', { params: { limit } })
  return requireApiData<TripReportSummary[]>(response.data, 'Trip report list request failed')
}

export async function getTripReport(reportId: string): Promise<TripReportDetail> {
  const response = await apiClient.get(`/api/reports/${reportId}`)
  return requireApiData<TripReportDetail>(response.data, 'Trip report detail request failed')
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
  const data = requireApiData<unknown>(response.data, 'Trip recalculation failed')
  return normalizePlan(data, {
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
    days: plan.days.map((day, index) => ({
      day_index: day.day_index || index + 1,
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

export async function askTravelQuestion(
  question: string,
  topK = 5,
  options: { conversation_id?: string | null; user_id?: string | null; anonymous_id?: string | null } = {},
): Promise<TravelQAResponse> {
  const response = await apiClient.post('/api/qa/ask', {
    question,
    top_k: topK,
    conversation_id: options.conversation_id || undefined,
    user_id: options.user_id || undefined,
    anonymous_id: options.anonymous_id || undefined,
  })
  return requireApiData<TravelQAResponse>(response.data, 'Travel QA request failed')
}

export async function streamTravelQuestion(
  question: string,
  options: {
    topK?: number
    conversation_id?: string | null
    user_id?: string | null
    anonymous_id?: string | null
    onStart?: (data: { conversation_id?: string | null; question?: string }) => void
    onDelta?: (content: string) => void
    onDone?: (response: TravelQAResponse) => void
  } = {},
): Promise<TravelQAResponse> {
  const authToken = localStorage.getItem(TOKEN_KEY)
  const fetchHeaders: Record<string, string> = { 'Content-Type': 'application/json' }
  if (authToken) fetchHeaders['Authorization'] = `Bearer ${authToken}`
  const response = await fetch(apiUrl('/api/qa/ask/stream'), {
    method: 'POST',
    headers: fetchHeaders,
    body: JSON.stringify({
      question,
      top_k: options.topK || 5,
      conversation_id: options.conversation_id || undefined,
      user_id: options.user_id || undefined,
      anonymous_id: options.anonymous_id || undefined,
    }),
  })
  if (!response.ok || !response.body) {
    throw new Error(`Travel QA stream failed: ${response.status}`)
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  let finalResponse: TravelQAResponse | null = null

  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const parts = buffer.split('\n\n')
    buffer = parts.pop() || ''
    for (const rawEvent of parts) {
      const parsed = parseSseEvent(rawEvent)
      if (!parsed) continue
      if (parsed.event === 'start') {
        options.onStart?.(parsed.data as { conversation_id?: string | null; question?: string })
      } else if (parsed.event === 'answer_delta') {
        const content = String((parsed.data as { content?: unknown }).content || '')
        if (content) options.onDelta?.(content)
      } else if (parsed.event === 'done') {
        finalResponse = parsed.data as TravelQAResponse
        options.onDone?.(finalResponse)
      } else if (parsed.event === 'error') {
        const message = String((parsed.data as { message?: unknown }).message || 'Travel QA stream failed')
        throw new Error(message)
      }
    }
  }

  if (!finalResponse) {
    throw new Error('Travel QA stream ended without final response')
  }
  return finalResponse
}

function parseSseEvent(rawEvent: string): { event: string; data: unknown } | null {
  const lines = rawEvent.split('\n')
  const eventLine = lines.find((line) => line.startsWith('event:'))
  const dataLine = lines.find((line) => line.startsWith('data:'))
  if (!eventLine || !dataLine) return null
  const event = eventLine.slice('event:'.length).trim()
  const dataText = dataLine.slice('data:'.length).trim()
  return { event, data: dataText ? JSON.parse(dataText) : {} }
}

export async function listQAConversations(options: {
  user_id?: string | null
  anonymous_id?: string | null
  limit?: number
} = {}): Promise<TravelQAConversationSummary[]> {
  const response = await apiClient.get('/api/qa/conversations', {
    params: {
      user_id: options.user_id || undefined,
      anonymous_id: options.anonymous_id || undefined,
      limit: options.limit || 50,
    },
  })
  return requireApiData<TravelQAConversationSummary[]>(response.data, 'Travel QA conversation list failed')
}

export async function getQAConversation(conversationId: string): Promise<TravelQAConversationDetail> {
  const response = await apiClient.get(`/api/qa/conversations/${conversationId}`)
  return requireApiData<TravelQAConversationDetail>(response.data, 'Travel QA conversation detail failed')
}

export async function ingestTravelNews(feedUrls: string[] = []): Promise<TravelNewsIngestResult> {
  const response = await apiClient.post('/api/news/ingest', {
    feed_urls: feedUrls,
  })
  return requireApiData<TravelNewsIngestResult>(response.data, 'Travel news ingest request failed')
}

export async function ingestTravelDocument(payload: TravelDocumentIngestPayload): Promise<TravelDocumentIngestResult> {
  const response = await apiClient.post('/api/knowledge/documents', payload)
  return requireApiData<TravelDocumentIngestResult>(response.data, 'Travel document ingest request failed')
}

export async function ingestTravelDocumentFromUrl(
  payload: TravelDocumentUrlIngestPayload,
): Promise<TravelDocumentIngestResult> {
  const response = await apiClient.post('/api/knowledge/documents/from-url', payload)
  return requireApiData<TravelDocumentIngestResult>(response.data, 'Travel document URL ingest request failed')
}

export async function ingestTravelDocumentAuto(
  payload: TravelDocumentAutoIngestPayload,
): Promise<TravelDocumentIngestResult> {
  const response = await apiClient.post('/api/knowledge/documents/auto', payload)
  return requireApiData<TravelDocumentIngestResult>(response.data, 'Travel document auto ingest request failed')
}

export async function createTravelDocumentUrlJob(
  payload: TravelDocumentUrlIngestPayload,
): Promise<TravelDocumentIngestJobResponse> {
  const response = await apiClient.post('/api/knowledge/documents/from-url/jobs', payload)
  return requireApiData<TravelDocumentIngestJobResponse>(response.data, 'Travel document URL job request failed')
}

export async function createTravelDocumentAutoJob(
  payload: TravelDocumentAutoIngestPayload,
): Promise<TravelDocumentIngestJobResponse> {
  const response = await apiClient.post('/api/knowledge/documents/auto/jobs', payload)
  return requireApiData<TravelDocumentIngestJobResponse>(response.data, 'Travel document auto job request failed')
}

export async function getTravelDocumentJob(jobId: string): Promise<TravelDocumentIngestJobStatus> {
  const response = await apiClient.get(`/api/knowledge/documents/jobs/${encodeURIComponent(jobId)}`)
  return requireApiData<TravelDocumentIngestJobStatus>(response.data, 'Travel document job status request failed')
}

export async function searchTravelDocuments(
  payload: TravelDocumentSearchPayload,
): Promise<TravelDocumentSearchResponse> {
  const response = await apiClient.post('/api/knowledge/search', payload)
  return requireApiData<TravelDocumentSearchResponse>(response.data, 'Travel document search request failed')
}

export async function getAttractionPhoto(
  name: string,
  options: { city?: string; report_id?: string | null } = {},
): Promise<string> {
  const response = await apiClient.get('/api/poi/photo', {
    params: {
      name,
      city: options.city || undefined,
      report_id: options.report_id || undefined,
    },
  })
  return requireApiData<{ photo_url: string }>(response.data, 'Attraction photo request failed').photo_url
}

export async function registerUser(username: string, password: string): Promise<AuthTokenResponse> {
  const response = await apiClient.post('/api/auth/register', { username, password })
  return requireApiData<AuthTokenResponse>(response.data, '注册失败')
}

export async function loginUser(username: string, password: string): Promise<AuthTokenResponse> {
  const response = await apiClient.post('/api/auth/login', { username, password })
  return requireApiData<AuthTokenResponse>(response.data, '登录失败')
}

export async function mergeAnonymousSessions(anonymousId: string): Promise<{ merged_count: number }> {
  const response = await apiClient.post('/api/auth/merge-anonymous', { anonymous_id: anonymousId })
  return requireApiData<{ merged_count: number }>(response.data, '匿名会话合并失败')
}

export async function getAuthMe(): Promise<AuthUser> {
  const response = await apiClient.get('/api/auth/me')
  return requireApiData<AuthUser>(response.data, '获取用户信息失败')
}

export default apiClient
