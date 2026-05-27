<template>
  <div class="result-container">
    <div class="result-header">
      <a-button size="large" @click="goBack">
        <template #icon><ArrowLeftOutlined /></template>
        返回首页
      </a-button>
      <a-space>
        <a-button v-if="!editMode" @click="startEdit">
          <template #icon><EditOutlined /></template>
          编辑行程
        </a-button>
        <a-button v-if="editMode" :loading="saving" @click="smartRefillDay">
          <template #icon><PlusOutlined /></template>
          智能补景点
        </a-button>
        <a-button v-if="editMode" :loading="saving" @click="smartReorderDay">
          <template #icon><SwapOutlined /></template>
          重排当天
        </a-button>
        <a-button v-if="editMode" type="primary" :loading="saving" @click="saveChanges">
          <template #icon><SaveOutlined /></template>
          保存修改
        </a-button>
        <a-button v-if="editMode" @click="cancelEdit">取消编辑</a-button>
      </a-space>
    </div>

    <a-empty v-if="!tripPlan" description="没有找到旅行计划数据">
      <a-button type="primary" @click="goBack">返回首页创建行程</a-button>
    </a-empty>

    <div v-else class="content-wrapper">
      <aside class="side-nav">
        <a-affix :offset-top="88">
          <a-menu mode="inline" :selected-keys="[activeSection]" @click="scrollToSection">
            <a-menu-item key="overview">行程概览</a-menu-item>
            <a-menu-item key="budget">预算明细</a-menu-item>
            <a-menu-item key="map">景点地图</a-menu-item>
            <a-sub-menu key="days" title="每日行程">
              <a-menu-item v-for="day in tripPlan.days" :key="`day-${day.day_index}`">
                第{{ day.day_index }}天
              </a-menu-item>
            </a-sub-menu>
            <a-menu-item key="weather">天气信息</a-menu-item>
          </a-menu>
        </a-affix>
      </aside>

      <main class="main-content">
        <a-card v-if="planningResult" title="方案对比" :bordered="false" class="option-section">
          <a-radio-group v-model:value="selectedOptionId" class="option-grid" @change="switchOption">
            <a-radio-button v-for="option in planningResult.options" :key="option.id" :value="option.id" class="option-card">
              <strong>{{ option.title }}</strong>
              <span>{{ option.suitable_for }}</span>
              <small>预算 ¥{{ option.plan.budget?.total || 0 }}</small>
            </a-radio-button>
          </a-radio-group>
          <div v-if="currentOption" class="option-detail">
            <a-tag color="blue">{{ currentOption.style }}</a-tag>
            <span v-for="item in currentOption.highlights" :key="item">{{ item }}</span>
          </div>
        </a-card>

        <a-card v-if="planningResult?.research_context.length" title="资料依据" :bordered="false" class="research-section">
          <a-list :data-source="planningResult.research_context" size="small">
            <template #renderItem="{ item }">
              <a-list-item>
                <a-list-item-meta :title="item.title" :description="item.summary" />
                <a-tag>{{ item.source }}</a-tag>
              </a-list-item>
            </template>
          </a-list>
        </a-card>

        <a-card v-if="planningResult?.quality_report" title="质量审校" :bordered="false" class="research-section">
          <a-space direction="vertical" style="width: 100%">
            <a-progress
              :percent="planningResult.quality_report.score"
              :status="planningResult.quality_report.warnings.length ? 'exception' : 'success'"
            />
            <a-alert
              v-for="warning in planningResult.quality_report.warnings"
              :key="warning"
              type="warning"
              show-icon
              :message="warning"
            />
            <div class="option-detail">
              <span v-for="item in planningResult.quality_report.recommendations" :key="item">{{ item }}</span>
            </div>
          </a-space>
        </a-card>

        <section class="top-info-section">
          <div class="left-info">
            <a-card id="overview" :title="`${tripPlan.city}旅行计划`" :bordered="false">
              <a-descriptions :column="1" size="small">
                <a-descriptions-item label="日期">{{ tripPlan.start_date }} 至 {{ tripPlan.end_date }}</a-descriptions-item>
                <a-descriptions-item label="生成方式">
                  {{ tripPlan.generation_mode === 'llm' ? 'LangChain 大模型规划' : '本地 fallback 规划' }}
                </a-descriptions-item>
                <a-descriptions-item label="建议">{{ tripPlan.overall_suggestions }}</a-descriptions-item>
              </a-descriptions>
            </a-card>

            <a-card id="budget" title="预算明细" :bordered="false">
              <div class="budget-grid">
                <div>
                  <span>景点门票</span>
                  <strong>¥{{ tripPlan.budget?.total_attractions || 0 }}</strong>
                </div>
                <div>
                  <span>酒店住宿</span>
                  <strong>¥{{ tripPlan.budget?.total_hotels || 0 }}</strong>
                </div>
                <div>
                  <span>餐饮费用</span>
                  <strong>¥{{ tripPlan.budget?.total_meals || 0 }}</strong>
                </div>
                <div>
                  <span>交通费用</span>
                  <strong>¥{{ tripPlan.budget?.total_transportation || 0 }}</strong>
                </div>
              </div>
              <div class="budget-total">
                <span>预计总费用</span>
                <strong>¥{{ tripPlan.budget?.total || 0 }}</strong>
              </div>
            </a-card>
          </div>

          <a-card id="map" title="地图点位" :bordered="false" class="map-card">
            <div v-if="mapMode === 'amap'" id="amap-container" class="amap-real"></div>
            <div v-else class="mock-map">
              <div
                v-for="pin in mapPins"
                :key="pin.id"
                :class="['mock-pin', `mock-pin-${pin.kind}`]"
                :style="{ left: `${pin.x}%`, top: `${pin.y}%` }"
                :title="pin.title"
              >
                {{ pin.label }}
              </div>
              <div class="mock-map-hint">{{ mapHint }}</div>
            </div>
            <div v-if="mapPointSummary.length" class="map-legend">
              <span v-for="item in mapPointSummary" :key="item.kind">
                <i :class="`map-legend-dot map-legend-dot-${item.kind}`"></i>{{ item.label }}
              </span>
            </div>
          </a-card>
        </section>

        <a-card title="每日行程" :bordered="false">
          <a-collapse v-model:activeKey="activeDays" accordion>
            <a-collapse-panel v-for="day in tripPlan.days" :key="day.day_index" :id="`day-${day.day_index}`">
              <template #header>
                <div class="day-header">
                  <span>第{{ day.day_index }}天</span>
                  <span>{{ day.date }}</span>
                </div>
              </template>

              <a-descriptions :column="1" size="small" class="day-info">
                <a-descriptions-item label="行程描述">{{ day.description }}</a-descriptions-item>
                <a-descriptions-item label="交通方式">{{ day.transportation }}</a-descriptions-item>
                <a-descriptions-item label="住宿偏好">{{ day.accommodation }}</a-descriptions-item>
              </a-descriptions>

              <a-divider orientation="left">景点安排</a-divider>
              <a-list :data-source="day.attractions" :grid="{ gutter: 16, xs: 1, sm: 1, md: 2, lg: 2 }">
                <template #renderItem="{ item, index }">
                  <a-list-item>
                    <a-card :title="item.name" size="small" class="attraction-card">
                      <template v-if="editMode" #extra>
                        <a-space>
                          <a-button size="small" :disabled="index === 0" @click="moveAttraction(day.day_index, index, 'up')">
                            上移
                          </a-button>
                          <a-button
                            size="small"
                            :disabled="index === day.attractions.length - 1"
                            @click="moveAttraction(day.day_index, index, 'down')"
                          >
                            下移
                          </a-button>
                          <a-button
                            size="small"
                            danger
                            :disabled="day.attractions.length <= 1"
                            @click="deleteAttraction(day.day_index, index)"
                          >
                            删除
                          </a-button>
                        </a-space>
                      </template>

                      <div class="attraction-image-wrapper">
                        <img :src="getAttractionImage(item, index)" :alt="item.name" class="attraction-image" />
                        <div class="attraction-badge">{{ index + 1 }}</div>
                        <div v-if="item.ticket_price !== undefined" class="price-tag">¥{{ item.ticket_price }}</div>
                      </div>

                      <template v-if="editMode">
                        <a-form layout="vertical">
                          <a-form-item label="地址">
                            <a-input v-model:value="item.address" />
                          </a-form-item>
                          <a-form-item label="游览时长">
                            <a-input-number v-model:value="item.visit_duration" :min="10" :max="480" style="width: 100%" />
                          </a-form-item>
                          <a-form-item label="描述">
                            <a-textarea v-model:value="item.description" :rows="2" />
                          </a-form-item>
                        </a-form>
                      </template>
                      <template v-else>
                        <p><strong>地址：</strong>{{ item.address }}</p>
                        <p><strong>游览时长：</strong>{{ item.visit_duration }}分钟</p>
                        <p><strong>描述：</strong>{{ item.description }}</p>
                      </template>
                    </a-card>
                  </a-list-item>
                </template>
              </a-list>

              <a-divider v-if="day.hotel" orientation="left">住宿推荐</a-divider>
              <a-card v-if="day.hotel" size="small" class="hotel-card">
                <a-descriptions :column="2" size="small">
                  <a-descriptions-item label="酒店">{{ day.hotel.name }}</a-descriptions-item>
                  <a-descriptions-item label="评分">{{ day.hotel.rating }}</a-descriptions-item>
                  <a-descriptions-item label="地址">{{ day.hotel.address }}</a-descriptions-item>
                  <a-descriptions-item label="价格">{{ day.hotel.price_range }}</a-descriptions-item>
                  <a-descriptions-item label="说明" :span="2">{{ day.hotel.distance }}</a-descriptions-item>
                </a-descriptions>
              </a-card>

              <a-divider orientation="left">餐饮安排</a-divider>
              <a-descriptions :column="1" bordered size="small">
                <a-descriptions-item v-for="meal in day.meals" :key="meal.type" :label="getMealLabel(meal.type)">
                  {{ meal.name }} <span v-if="meal.description">- {{ meal.description }}</span>
                  <span v-if="meal.address">｜{{ meal.address }}</span>
                  <span v-if="meal.rating">｜评分 {{ meal.rating }}</span>
                  <span v-if="meal.estimated_cost">（约¥{{ meal.estimated_cost }}）</span>
                </a-descriptions-item>
              </a-descriptions>
            </a-collapse-panel>
          </a-collapse>
        </a-card>

        <a-card id="weather" title="天气信息" :bordered="false" class="weather-section">
          <a-list :data-source="tripPlan.weather_info" :grid="{ gutter: 16, xs: 1, sm: 2, md: 3 }">
            <template #renderItem="{ item }">
              <a-list-item>
                <a-card size="small" class="weather-card">
                  <div class="weather-date">{{ item.date }}</div>
                  <div>白天：{{ item.day_weather }} {{ item.day_temp }}°C</div>
                  <div>夜间：{{ item.night_weather }} {{ item.night_temp }}°C</div>
                  <div>{{ item.wind_direction }} {{ item.wind_power }}</div>
                </a-card>
              </a-list-item>
            </template>
          </a-list>
        </a-card>
      </main>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import {
  ArrowLeftOutlined,
  EditOutlined,
  PlusOutlined,
  SaveOutlined,
  SwapOutlined,
} from '@ant-design/icons-vue'
import { getAttractionPhoto, recalculateTripPlan } from '@/services/api'
import travelHeroUrl from '@/assets/travel-qa-hero.webp'
import type { Attraction, Hotel, Location, Meal, TripPlanningResult, TripPlan } from '@/types'

type MapMode = 'amap' | 'mock'
type MapPointKind = 'attraction' | 'hotel' | 'meal'

interface MapPoint {
  id: string
  kind: MapPointKind
  name: string
  title: string
  label: string
  address?: string
  location: Location
  detailLines: string[]
}

const router = useRouter()
const planningResult = ref<TripPlanningResult | null>(null)
const tripPlan = ref<TripPlan | null>(null)
const originalPlan = ref<TripPlan | null>(null)
const selectedOptionId = ref('balanced')
const editMode = ref(false)
const saving = ref(false)
const activeSection = ref('overview')
const activeDays = ref<number[]>([1])
const attractionPhotos = ref<Record<string, string>>({})
const mapMode = ref<MapMode>('mock')
const mapHint = ref('未配置高德地图 Web JS Key，当前显示路线示意图')

let amapInstance: any = null
let AMapRuntime: any = null

onMounted(async () => {
  const resultData = sessionStorage.getItem('tripPlanningResult')
  const planData = sessionStorage.getItem('tripPlan')
  if (resultData) {
    planningResult.value = JSON.parse(resultData)
    selectedOptionId.value = planningResult.value?.selected_option_id || 'balanced'
    const selectedPlan = currentOption.value?.plan || planningResult.value?.options[0]?.plan || null
    tripPlan.value = selectedPlan ? JSON.parse(JSON.stringify(selectedPlan)) : null
    if (tripPlan.value) sessionStorage.setItem('tripPlan', JSON.stringify(tripPlan.value))
    await loadAttractionPhotos()
    await nextTick()
    await initAmap()
  } else if (planData) {
    tripPlan.value = JSON.parse(planData)
    await loadAttractionPhotos()
    await nextTick()
    await initAmap()
  }
})

onBeforeUnmount(() => {
  destroyMap()
})

watch(
  () => tripPlan.value?.days,
  async () => {
    await nextTick()
    renderAmapMarkers()
  },
  { deep: true },
)

function goBack() {
  router.push('/')
}

function scrollToSection({ key }: { key: string }) {
  activeSection.value = key
  const element = document.getElementById(key)
  if (element) element.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

const currentOption = computed(() => {
  return planningResult.value?.options.find((option) => option.id === selectedOptionId.value)
})

async function switchOption() {
  const option = currentOption.value
  if (!option) return
  tripPlan.value = JSON.parse(JSON.stringify(option.plan))
  sessionStorage.setItem('tripPlan', JSON.stringify(tripPlan.value))
  if (planningResult.value) {
    planningResult.value.selected_option_id = option.id
    sessionStorage.setItem('tripPlanningResult', JSON.stringify(planningResult.value))
  }
  await nextTick()
  renderAmapMarkers()
}

function startEdit() {
  editMode.value = true
  originalPlan.value = JSON.parse(JSON.stringify(tripPlan.value))
}

async function saveChanges() {
  if (!tripPlan.value) return
  saving.value = true
  try {
    tripPlan.value = await recalculateTripPlan(tripPlan.value, {
      report_id: planningResult.value?.report_id,
      research_context: planningResult.value?.research_context || [],
    })
    syncSelectedOption()
    sessionStorage.setItem('tripPlan', JSON.stringify(tripPlan.value))
    editMode.value = false
    await nextTick()
    renderAmapMarkers()
    message.success('修改已保存，预算和地图已更新')
  } catch (error: any) {
    message.error(error.message || '保存失败')
  } finally {
    saving.value = false
  }
}

function cancelEdit() {
  if (originalPlan.value) tripPlan.value = JSON.parse(JSON.stringify(originalPlan.value))
  editMode.value = false
}

function deleteAttraction(dayIndex: number, attrIndex: number) {
  const day = tripPlan.value?.days.find((item) => item.day_index === dayIndex)
  if (!day || day.attractions.length <= 1) return
  day.attractions.splice(attrIndex, 1)
}

function moveAttraction(dayIndex: number, attrIndex: number, direction: 'up' | 'down') {
  const day = tripPlan.value?.days.find((item) => item.day_index === dayIndex)
  if (!day) return
  const nextIndex = direction === 'up' ? attrIndex - 1 : attrIndex + 1
  if (nextIndex < 0 || nextIndex >= day.attractions.length) return
  ;[day.attractions[attrIndex], day.attractions[nextIndex]] = [day.attractions[nextIndex], day.attractions[attrIndex]]
}

async function smartRefillDay() {
  await smartAdjustDay('refill_day')
}

async function smartReorderDay() {
  await smartAdjustDay('reorder_day')
}

async function smartAdjustDay(operation: 'refill_day' | 'reorder_day') {
  if (!tripPlan.value) return
  saving.value = true
  try {
    const activeDay = Number(activeDays.value[0] ?? 1)
    tripPlan.value = await recalculateTripPlan(tripPlan.value, {
      report_id: planningResult.value?.report_id,
      operation,
      day_index: activeDay,
      research_context: planningResult.value?.research_context || [],
    })
    syncSelectedOption()
    sessionStorage.setItem('tripPlan', JSON.stringify(tripPlan.value))
    await nextTick()
    renderAmapMarkers()
    message.success(operation === 'refill_day' ? '已智能补充当天景点' : '已重排当天路线')
  } catch (error: any) {
    message.error(error.message || '智能调整失败')
  } finally {
    saving.value = false
  }
}

function syncSelectedOption() {
  if (!planningResult.value || !tripPlan.value) return
  const option = planningResult.value.options.find((item) => item.id === selectedOptionId.value)
  if (option) option.plan = tripPlan.value
  sessionStorage.setItem('tripPlanningResult', JSON.stringify(planningResult.value))
}

function getMealLabel(type: string) {
  const labels: Record<string, string> = {
    breakfast: '早餐',
    lunch: '午餐',
    dinner: '晚餐',
    snack: '小吃',
  }
  return labels[type] || type
}

async function loadAttractionPhotos() {
  if (!tripPlan.value) return
  const tasks = tripPlan.value.days
    .flatMap((day) => day.attractions)
    .map((attraction) => async () => {
      if (attraction.image_url && !isRetiredImageUrl(attraction.image_url)) {
        attractionPhotos.value[attraction.name] = attraction.image_url
        return
      }
      try {
        attractionPhotos.value[attraction.name] = await getAttractionPhoto(attraction.name)
      } catch {
        attractionPhotos.value[attraction.name] = ''
      }
    })
  await runLimited(tasks, 4)
}

async function runLimited(tasks: Array<() => Promise<void>>, limit: number) {
  for (let index = 0; index < tasks.length; index += limit) {
    await Promise.all(tasks.slice(index, index + limit).map((task) => task()))
  }
}

function isRetiredImageUrl(url?: string) {
  return !url || url.includes('source.unsplash.com')
}

function getAttractionImage(item: Attraction, index: number) {
  if (item.image_url && !isRetiredImageUrl(item.image_url)) return item.image_url
  if (attractionPhotos.value[item.name]) return attractionPhotos.value[item.name]
  return travelHeroUrl
}

async function initAmap() {
  const key = import.meta.env.VITE_AMAP_WEB_JS_KEY
  const securityJsCode = import.meta.env.VITE_AMAP_SECURITY_JS_CODE
  if (!key) {
    mapMode.value = 'mock'
    mapHint.value = '未配置高德地图 Web JS Key，当前显示路线示意图'
    return
  }

  try {
    if (securityJsCode) {
      ;(window as any)._AMapSecurityConfig = { securityJsCode }
    }
    mapMode.value = 'amap'
    await nextTick()
    const { default: AMapLoader } = await import('@amap/amap-jsapi-loader')
    AMapRuntime = await AMapLoader.load({
      key,
      version: '2.0',
      plugins: ['AMap.Marker', 'AMap.Polyline', 'AMap.InfoWindow'],
    })
    amapInstance = new AMapRuntime.Map('amap-container', {
      zoom: 12,
      center: mapCenter.value,
      viewMode: '3D',
    })
    renderAmapMarkers()
  } catch (error) {
    console.error('高德地图加载失败:', error)
    destroyMap()
    mapMode.value = 'mock'
    mapHint.value = '高德地图加载失败，当前显示路线示意图'
  }
}

function renderAmapMarkers() {
  if (!amapInstance || !AMapRuntime || !tripPlan.value) return
  amapInstance.clearMap()

  const attractions = allAttractions.value
  const markers = allMapPoints.value.map((point) => {
    const marker = new AMapRuntime.Marker({
      position: [point.location.longitude, point.location.latitude],
      title: point.title,
      label: {
        content: `<div class="amap-label amap-label-${point.kind}">${escapeHtml(point.label)}</div>`,
        offset: new AMapRuntime.Pixel(0, -28),
      },
    })
    const infoWindow = new AMapRuntime.InfoWindow({
      content: buildInfoWindowContent(point),
      offset: new AMapRuntime.Pixel(0, -32),
    })
    marker.on('click', () => infoWindow.open(amapInstance, marker.getPosition()))
    return marker
  })

  if (markers.length > 0) amapInstance.add(markers)
  drawAmapRoutes(attractions)
  if (markers.length > 0) amapInstance.setFitView(markers)
}

function buildInfoWindowContent(point: MapPoint) {
  const lines = [point.address, ...point.detailLines].filter(Boolean).map((line) => escapeHtml(line || ''))
  return `<div style="padding:8px 10px;max-width:260px"><strong>${escapeHtml(point.name)}</strong><br/>${lines.join('<br/>')}</div>`
}

function drawAmapRoutes(attractions: Attraction[]) {
  if (!amapInstance || !AMapRuntime || attractions.length < 2) return
  const dayGroups = tripPlan.value?.days || []
  dayGroups.forEach((day) => {
    if (day.attractions.length < 2) return
    const path = day.attractions.map((attraction) => [attraction.location.longitude, attraction.location.latitude])
    amapInstance.add(
      new AMapRuntime.Polyline({
        path,
        strokeColor: '#1a3c34',
        strokeWeight: 4,
        strokeOpacity: 0.72,
        showDir: true,
      }),
    )
  })
}

function destroyMap() {
  if (amapInstance) {
    amapInstance.destroy()
    amapInstance = null
  }
}

const allAttractions = computed(() => {
  return tripPlan.value?.days.flatMap((day) => day.attractions) || []
})

const allMapPoints = computed<MapPoint[]>(() => {
  if (!tripPlan.value) return []

  const points: MapPoint[] = []
  const seenHotels = new Set<string>()
  let attractionIndex = 0

  tripPlan.value.days.forEach((day) => {
    day.attractions.forEach((attraction) => {
      if (!isValidLocation(attraction.location)) return
      attractionIndex += 1
      points.push({
        id: `attraction-${day.day_index}-${attractionIndex}-${attraction.name}`,
        kind: 'attraction',
        name: attraction.name,
        title: `D${day.day_index} ${attraction.name}`,
        label: String(attractionIndex),
        address: attraction.address,
        location: attraction.location,
        detailLines: [`D${day.day_index}`, `游览 ${attraction.visit_duration} 分钟`],
      })
    })

    const hotelPoint = createHotelPoint(day.hotel, day.day_index, seenHotels)
    if (hotelPoint) points.push(hotelPoint)

    day.meals.forEach((meal) => {
      const mealPoint = createMealPoint(meal, day.day_index)
      if (mealPoint) points.push(mealPoint)
    })
  })

  return points
})

const mapCenter = computed<[number, number]>(() => {
  const first = allMapPoints.value[0]
  return first ? [first.location.longitude, first.location.latitude] : [116.397128, 39.916527]
})

const mapPins = computed(() => {
  const points = allMapPoints.value
  if (points.length === 0) return []
  const lngs = points.map((item) => item.location.longitude)
  const lats = points.map((item) => item.location.latitude)
  const minLng = Math.min(...lngs)
  const maxLng = Math.max(...lngs)
  const minLat = Math.min(...lats)
  const maxLat = Math.max(...lats)
  const lngSpan = maxLng - minLng || 1
  const latSpan = maxLat - minLat || 1
  return points.map((item) => ({
    id: item.id,
    kind: item.kind,
    name: item.name,
    title: item.title,
    label: item.label,
    x: 10 + ((item.location.longitude - minLng) / lngSpan) * 80,
    y: 90 - ((item.location.latitude - minLat) / latSpan) * 80,
  }))
})

const mapPointSummary = computed(() => {
  const kinds = new Set(allMapPoints.value.map((point) => point.kind))
  return [
    { kind: 'attraction', label: '景点' },
    { kind: 'hotel', label: '酒店' },
    { kind: 'meal', label: '餐饮' },
  ].filter((item) => kinds.has(item.kind as MapPointKind))
})

function createHotelPoint(hotel: Hotel | undefined, dayIndex: number, seenHotels: Set<string>): MapPoint | null {
  if (!hotel?.location || !isValidLocation(hotel.location)) return null
  const hotelKey = `${hotel.name}-${hotel.location.longitude}-${hotel.location.latitude}`
  if (seenHotels.has(hotelKey)) return null
  seenHotels.add(hotelKey)
  return {
    id: `hotel-${dayIndex}-${hotel.name}`,
    kind: 'hotel',
    name: hotel.name,
    title: `D${dayIndex} ${hotel.name}`,
    label: `H${dayIndex}`,
    address: hotel.address,
    location: hotel.location,
    detailLines: [`D${dayIndex} 酒店`, hotel.price_range, hotel.rating ? `评分 ${hotel.rating}` : ''],
  }
}

function createMealPoint(meal: Meal, dayIndex: number): MapPoint | null {
  if (!meal.location || !isValidLocation(meal.location)) return null
  return {
    id: `meal-${dayIndex}-${meal.type}-${meal.name}`,
    kind: 'meal',
    name: meal.name,
    title: `D${dayIndex} ${getMealLabel(meal.type)}`,
    label: mealMarkerLabel(meal.type),
    address: meal.address,
    location: meal.location,
    detailLines: [
      `D${dayIndex} ${getMealLabel(meal.type)}`,
      meal.estimated_cost ? `约 ¥${meal.estimated_cost}` : '',
      meal.rating ? `评分 ${meal.rating}` : '',
    ],
  }
}

function mealMarkerLabel(type: string) {
  const labels: Record<string, string> = {
    breakfast: 'B',
    lunch: 'L',
    dinner: 'D',
    snack: 'S',
  }
  return labels[type] || 'M'
}

function isValidLocation(location: Location | undefined | null): location is Location {
  return Boolean(location && Number.isFinite(location.longitude) && Number.isFinite(location.latitude))
}

function escapeHtml(value: string) {
  return value.replace(/[&<>"']/g, (char) => {
    const entities: Record<string, string> = {
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;',
    }
    return entities[char]
  })
}
</script>

<style scoped>
.result-container {
  padding: 24px;
  background: var(--color-cream);
}

.result-header {
  max-width: 1440px;
  margin: 0 auto 28px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  animation: fadeInUp 0.5s ease-out;
}

.content-wrapper {
  max-width: 1440px;
  margin: 0 auto;
  display: flex;
  gap: 28px;
}

.side-nav {
  width: 240px;
  flex-shrink: 0;
}

.main-content {
  flex: 1;
  min-width: 0;
}

.top-info-section {
  display: grid;
  grid-template-columns: 420px minmax(0, 1fr);
  gap: 24px;
  margin-bottom: 24px;
}

.left-info {
  display: flex;
  flex-direction: column;
  gap: 24px;
}

@media (max-width: 1100px) {
  .content-wrapper,
  .top-info-section {
    display: block;
  }

  .side-nav {
    display: none;
  }

  .left-info,
  .map-card {
    margin-bottom: 24px;
  }
}

@media (max-width: 768px) {
  .result-container {
    padding: 16px;
  }

  .result-header {
    flex-direction: column;
    align-items: stretch;
    gap: 14px;
  }
}
</style>
