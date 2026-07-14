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
  id?: string
  type: 'breakfast' | 'lunch' | 'dinner' | 'snack'
  name: string
  address?: string
  location?: Location
  description?: string
  estimated_cost?: number
  rating?: number
  category?: string
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

export interface QualityReport {
  score: number
  warnings: string[]
  recommendations: string[]
}

export interface TripPlanningResult {
  selected_option_id: string
  options: TripPlanOption[]
  research_context: ResearchSnippet[]
  clarifying_suggestions: string[]
  quality_report?: QualityReport
  report_id?: string | null
  report_created_at?: string | null
  report_updated_at?: string | null
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

export interface TripReportSummary {
  id: string
  prompt: string
  city: string
  days_count: number
  budget_total: number
  generation_mode: string
  created_at: string
  updated_at: string
}

export interface TripReportRevision {
  id: string
  report_id: string
  operation: string
  plan: Record<string, unknown>
  research_context: ResearchSnippet[]
  budget_total: number
  created_at: string
}

export interface TripReportDetail extends TripReportSummary {
  request: Record<string, unknown>
  result: Record<string, unknown>
  selected_plan: Record<string, unknown>
  revisions: TripReportRevision[]
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
  amap_transport?: string
  unsplash_configured: boolean
  planner_mode?: string
  cache_enabled?: boolean
  external_api_disabled: boolean
  image_providers?: {
    web_search?: boolean
    llm_selector?: boolean
    wikimedia: boolean
    openverse: boolean
    pexels_configured: boolean
    pixabay_configured: boolean
    unsplash_configured: boolean
  }
  database?: {
    enabled: boolean
    ok: boolean
    error?: string
  }
  travel_knowledge?: {
    enabled: boolean
    ok: boolean
    pgvector_enabled?: boolean
    table_ready?: boolean
    error?: string
  }
  qa_memory?: {
    enabled: boolean
    ok: boolean
    memory_only?: boolean
    error?: string
  }
  web_search?: {
    enabled: boolean
    tool: string
  }
}

export interface TravelKnowledgeSource {
  title: string
  url?: string | null
  summary: string
  source: string
  published_at?: string | null
  score: number
}

export interface TravelQAResponse {
  answer: string
  sources: TravelKnowledgeSource[]
  retrieved_count: number
  generation_mode: 'llm' | 'fallback'
  used_web_search: boolean
  conversation_id?: string | null
  message_id?: string | null
}

export interface TravelQAChatMessage {
  id: string
  conversation_id: string
  role: 'user' | 'assistant'
  content: string
  sources: TravelKnowledgeSource[]
  retrieved_count: number
  generation_mode?: 'llm' | 'fallback' | null
  used_web_search?: boolean
  created_at: string
}

export interface TravelQAConversationSummary {
  id: string
  title: string
  user_id?: string | null
  anonymous_id?: string | null
  created_at: string
  updated_at: string
}

export interface TravelQAConversationDetail extends TravelQAConversationSummary {
  messages: TravelQAChatMessage[]
}

export interface TravelFeedIngestStats {
  url: string
  seen: number
  added: number
}

export interface TravelNewsIngestResult {
  total_seen: number
  total_added: number
  feeds: TravelFeedIngestStats[]
  errors: string[]
}

export interface TravelDocumentIngestPayload {
  title: string
  content: string
  source_name: string
  source_url?: string | null
  source_type?: string
  province?: string | null
  city?: string | null
  data_type?: string | null
  scenic_spot?: string | null
  publish_date?: string | null
  metadata?: Record<string, unknown>
}

export interface TravelDocumentUrlIngestPayload {
  source_url: string
  title?: string | null
  source_name?: string
  source_type?: string
  province?: string | null
  city?: string | null
  data_type?: string | null
  scenic_spot?: string | null
  publish_date?: string | null
  metadata?: Record<string, unknown>
}

export interface TravelDocumentAutoIngestPayload {
  content: string
  file_name?: string | null
  source_url?: string | null
  source_type?: string
}

export interface TravelDocumentIngestResult {
  doc_id: string
  chunks_added: number
}

export interface TravelDocumentIngestJobResponse {
  job_id: string
  status: 'queued' | 'running' | 'completed' | 'failed'
  message: string
}

export interface TravelDocumentIngestJobStatus {
  job_id: string
  status: 'queued' | 'running' | 'completed' | 'failed'
  message: string
  result?: TravelDocumentIngestResult | null
  error?: string | null
  created_at: string
  updated_at: string
}

export interface TravelDocumentSearchPayload {
  query: string
  province?: string | null
  city?: string | null
  data_type?: string | null
  source_type?: string | null
  source_name?: string | null
  publish_date_from?: string | null
  publish_date_to?: string | null
  top_k: number
}

export interface TravelDocumentSearchResult {
  chunk_id: string
  title: string
  section: string
  content: string
  source_name: string
  source_url?: string | null
  publish_date?: string | null
  score: number
  metadata: Record<string, unknown>
}

export interface TravelDocumentSearchResponse {
  query: string
  results: TravelDocumentSearchResult[]
}

export interface AuthTokenResponse {
  access_token: string
  token_type: 'bearer'
  user_id: string
  username: string
}

export interface PrincipalTokenResponse {
  access_token: string
  token_type: 'bearer'
  principal_type: 'anonymous' | 'user'
  subject: string
  expires_in: number
}

export interface AnonymousMergeResponse {
  merged_conversations: number
  merged_messages: number
  anonymous_id: string
}

export interface AuthUser {
  user_id: string
  username: string
}
