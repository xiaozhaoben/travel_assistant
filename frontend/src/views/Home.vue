<template>
  <div class="home-container">
    <div class="page-header">
      <div class="icon-wrapper"><CompassOutlined /></div>
      <h1 class="page-title">旅行规划工作台</h1>
      <p class="page-subtitle">规划行程 · 沉淀旅行资讯 · 回答目的地问题</p>
    </div>

    <a-card class="form-card" :bordered="false">
      <div class="service-panel">
        <div>
          <div class="service-title">服务状态</div>
          <div class="service-subtitle">{{ serviceStatusText }}</div>
        </div>
        <div class="service-tags">
          <a-tag :color="serviceHealth?.llm.enabled && !serviceHealth?.llm.disabled ? 'green' : 'orange'">
            大模型：{{ serviceHealth?.llm.enabled && !serviceHealth?.llm.disabled ? serviceHealth.llm.model : 'Fallback' }}
          </a-tag>
          <a-tag :color="serviceHealth?.amap_configured && !serviceHealth?.external_api_disabled ? 'green' : 'orange'">
            高德地图：{{ serviceHealth?.amap_configured && !serviceHealth?.external_api_disabled ? '已配置' : '本地数据' }}
          </a-tag>
          <a-tag :color="serviceHealth?.unsplash_configured && !serviceHealth?.external_api_disabled ? 'green' : 'orange'">
            Unsplash：{{ serviceHealth?.unsplash_configured && !serviceHealth?.external_api_disabled ? '已配置' : '占位图片' }}
          </a-tag>
          <a-tag :color="serviceHealth?.travel_knowledge?.enabled ? 'green' : 'orange'">
            知识库：{{ serviceHealth?.travel_knowledge?.enabled ? 'PostgreSQL' : '未配置' }}
          </a-tag>
          <a-tag :color="serviceHealth?.web_search?.enabled ? 'green' : 'orange'">
            实时搜索：{{ serviceHealth?.web_search?.enabled ? serviceHealth.web_search.tool : '未配置' }}
          </a-tag>
        </div>
      </div>

      <a-form :model="formData" layout="vertical" @finish="handleSubmit">
        <div class="form-section">
          <div class="section-header">
            <span class="section-icon">📍</span>
            <span class="section-title">目的地与日期</span>
          </div>

          <a-row :gutter="[24, 16]">
            <a-col :xs="24" :md="8">
              <a-form-item name="city" label="目的地城市" :rules="[{ required: true, message: '请输入目的地城市' }]">
                <a-input v-model:value="formData.city" size="large" placeholder="例如：北京" />
              </a-form-item>
            </a-col>
            <a-col :xs="24" :md="6">
              <a-form-item name="start_date" label="开始日期" :rules="[{ required: true, message: '请选择开始日期' }]">
                <a-date-picker v-model:value="dateRange.start" size="large" style="width: 100%" />
              </a-form-item>
            </a-col>
            <a-col :xs="24" :md="6">
              <a-form-item name="end_date" label="结束日期" :rules="[{ required: true, message: '请选择结束日期' }]">
                <a-date-picker v-model:value="dateRange.end" size="large" style="width: 100%" />
              </a-form-item>
            </a-col>
            <a-col :xs="24" :md="4">
              <a-form-item label="旅行天数">
                <div class="days-display">
                  <strong>{{ formData.travel_days }}</strong>
                  <span>天</span>
                </div>
              </a-form-item>
            </a-col>
          </a-row>
        </div>

        <div class="form-section">
          <div class="section-header">
            <span class="section-icon">🧭</span>
            <span class="section-title">方案细化</span>
          </div>

          <a-row :gutter="[24, 16]">
            <a-col :xs="24" :md="6">
              <a-form-item label="旅行节奏">
                <a-select v-model:value="formData.travel_style" size="large">
                  <a-select-option value="经典均衡">经典均衡</a-select-option>
                  <a-select-option value="轻松舒适">轻松舒适</a-select-option>
                  <a-select-option value="深度探索">深度探索</a-select-option>
                </a-select>
              </a-form-item>
            </a-col>
            <a-col :xs="24" :md="6">
              <a-form-item label="同行人群">
                <a-select v-model:value="formData.companions" size="large">
                  <a-select-option value="朋友">朋友</a-select-option>
                  <a-select-option value="情侣">情侣</a-select-option>
                  <a-select-option value="亲子">亲子</a-select-option>
                  <a-select-option value="老人">老人</a-select-option>
                  <a-select-option value="独自旅行">独自旅行</a-select-option>
                </a-select>
              </a-form-item>
            </a-col>
            <a-col :xs="24" :md="6">
              <a-form-item label="餐饮偏好">
                <a-input v-model:value="formData.food_preferences" size="large" placeholder="本地菜、清淡、素食" />
              </a-form-item>
            </a-col>
            <a-col :xs="24" :md="6">
              <a-form-item label="低强度">
                <a-switch v-model:checked="formData.low_intensity" checked-children="是" un-checked-children="否" />
              </a-form-item>
            </a-col>
            <a-col :xs="24" :md="12">
              <a-form-item label="必去地点">
                <a-input v-model:value="formData.must_visit" size="large" placeholder="用逗号分隔，例如故宫、天坛" />
              </a-form-item>
            </a-col>
            <a-col :xs="24" :md="12">
              <a-form-item label="避开地点">
                <a-input v-model:value="formData.avoid_places" size="large" placeholder="用逗号分隔，例如商场、远郊" />
              </a-form-item>
            </a-col>
          </a-row>
        </div>

        <div class="form-section">
          <div class="section-header">
            <span class="section-icon">⚙️</span>
            <span class="section-title">偏好设置</span>
          </div>

          <a-row :gutter="[24, 16]">
            <a-col :xs="24" :md="8">
              <a-form-item label="交通方式">
                <a-select v-model:value="formData.transportation" size="large">
                  <a-select-option value="公共交通">公共交通</a-select-option>
                  <a-select-option value="自驾">自驾</a-select-option>
                  <a-select-option value="步行">步行</a-select-option>
                  <a-select-option value="混合">混合</a-select-option>
                </a-select>
              </a-form-item>
            </a-col>
            <a-col :xs="24" :md="8">
              <a-form-item label="住宿偏好">
                <a-select v-model:value="formData.accommodation" size="large">
                  <a-select-option value="经济型酒店">经济型酒店</a-select-option>
                  <a-select-option value="舒适型酒店">舒适型酒店</a-select-option>
                  <a-select-option value="豪华酒店">豪华酒店</a-select-option>
                  <a-select-option value="民宿">民宿</a-select-option>
                </a-select>
              </a-form-item>
            </a-col>
            <a-col :xs="24" :md="8">
              <a-form-item label="旅行偏好">
                <a-checkbox-group v-model:value="formData.preferences" class="preference-tags">
                  <a-checkbox value="历史文化">历史文化</a-checkbox>
                  <a-checkbox value="自然风光">自然风光</a-checkbox>
                  <a-checkbox value="美食">美食</a-checkbox>
                  <a-checkbox value="购物">购物</a-checkbox>
                  <a-checkbox value="艺术">艺术</a-checkbox>
                  <a-checkbox value="休闲">休闲</a-checkbox>
                </a-checkbox-group>
              </a-form-item>
            </a-col>
          </a-row>
        </div>

        <div class="form-section">
          <div class="section-header">
            <span class="section-icon">💬</span>
            <span class="section-title">额外要求</span>
          </div>
          <a-form-item>
            <a-textarea
              v-model:value="formData.free_text_input"
              :rows="3"
              size="large"
              placeholder="例如：想看升旗、需要少走路、对海鲜过敏、希望安排博物馆"
            />
          </a-form-item>
        </div>

        <a-button type="primary" html-type="submit" :loading="loading" size="large" block class="submit-button">
          {{ loading ? '正在生成中...' : '开始规划我的旅行' }}
        </a-button>

        <div v-if="loading" class="loading-container">
          <a-progress :percent="loadingProgress" status="active" />
          <p>{{ loadingStatus }}</p>
        </div>
      </a-form>
    </a-card>

    <div class="side-action-buttons">
      <a-button type="primary" class="side-action-button" @click="qaModalOpen = true">
        <template #icon><MessageOutlined /></template>
        <span>智能问答</span>
      </a-button>
      <a-button type="primary" class="side-action-button report-action-button" @click="router.push('/reports')">
        <template #icon><FileTextOutlined /></template>
        <span>历史报表</span>
      </a-button>
    </div>

    <a-modal v-model:open="qaModalOpen" width="920px" :footer="null" centered wrap-class-name="qa-modal">
      <template #title>
        <div class="qa-modal-title">
          <MessageOutlined />
          <span>旅行智能问答</span>
        </div>
      </template>
      <div class="qa-console">
        <div class="qa-input-panel">
          <div class="section-header compact">
            <span class="section-icon"><MessageOutlined /></span>
            <span class="section-title">问目的地、预约、交通与避坑</span>
          </div>
          <a-textarea
            v-model:value="qaQuestion"
            :rows="5"
            size="large"
            placeholder="例如：端午去南京有哪些预约和错峰建议？"
            @press-enter.ctrl="handleAskQuestion"
          />
          <div class="qa-actions">
            <a-button type="primary" :loading="qaLoading" @click="handleAskQuestion">
              <template #icon><SendOutlined /></template>
              提问
            </a-button>
            <a-button :loading="newsIngesting" @click="handleIngestNews">
              <template #icon><SyncOutlined /></template>
              更新旅行资讯
            </a-button>
          </div>
          <div v-if="ingestSummary" class="ingest-summary">
            <DatabaseOutlined />
            {{ ingestSummary }}
          </div>
        </div>
        <div class="qa-answer-panel">
          <a-empty v-if="!qaAnswer" description="旅行问答会显示在这里" />
          <template v-else>
            <div class="qa-answer-text">{{ qaAnswer.answer }}</div>
          </template>
        </div>
      </div>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import {
  CompassOutlined,
  DatabaseOutlined,
  FileTextOutlined,
  MessageOutlined,
  SendOutlined,
  SyncOutlined,
} from '@ant-design/icons-vue'
import dayjs, { type Dayjs } from 'dayjs'
import { askTravelQuestion, generateTripPlan, healthCheck, ingestTravelNews } from '@/services/api'
import type { ServiceHealth, TravelQAResponse, TripFormData } from '@/types'

const router = useRouter()
const loading = ref(false)
const loadingProgress = ref(0)
const loadingStatus = ref('')
const serviceHealth = ref<ServiceHealth | null>(null)
const qaModalOpen = ref(false)
const qaQuestion = ref('端午去南京三天，想看历史文化和夜景，有哪些预约和错峰建议？')
const qaAnswer = ref<TravelQAResponse | null>(null)
const qaLoading = ref(false)
const newsIngesting = ref(false)
const ingestSummary = ref('')
const serviceStatusText = computed(() => {
  if (!serviceHealth.value) return '正在连接后端服务...'
  if (serviceHealth.value.external_api_disabled) return '当前强制使用本地 fallback 数据，适合离线调试。'
  if (serviceHealth.value.llm.enabled) return '后端已读取大模型配置，规划时会优先调用大模型。'
  return '后端未检测到大模型 Key，会使用本地 fallback 生成可编辑行程。'
})

async function handleAskQuestion() {
  const question = qaQuestion.value.trim()
  if (question.length < 2) {
    message.warning('请输入旅行相关问题')
    return
  }
  qaLoading.value = true
  try {
    qaAnswer.value = await askTravelQuestion(question)
  } catch (error: any) {
    message.error(error.message || '智能问答失败')
  } finally {
    qaLoading.value = false
  }
}

async function handleIngestNews() {
  newsIngesting.value = true
  ingestSummary.value = ''
  try {
    const result = await ingestTravelNews()
    if (result.total_seen > 0 && result.total_added === 0) {
      ingestSummary.value = `已读取 ${result.total_seen} 条，未新增知识片段，内容可能已在库中`
    } else if (result.total_seen === 0) {
      ingestSummary.value = '本次没有读取到可用 RSS 条目'
    } else {
      ingestSummary.value = `已读取 ${result.total_seen} 条，新增 ${result.total_added} 个知识片段`
    }
    if (result.errors.length) {
      message.warning(`部分 RSS 源失败：${result.errors[0]}`)
    } else {
      message.success('旅行资讯已更新')
    }
  } catch (error: any) {
    message.error(error.message || '旅行资讯入库失败')
  } finally {
    newsIngesting.value = false
  }
}

const dateRange = reactive<{ start: Dayjs | null; end: Dayjs | null }>({
  start: dayjs().add(7, 'day'),
  end: dayjs().add(9, 'day'),
})

const formData = reactive<TripFormData>({
  city: '北京',
  start_date: dateRange.start!.format('YYYY-MM-DD'),
  end_date: dateRange.end!.format('YYYY-MM-DD'),
  travel_days: 3,
  transportation: '公共交通',
  accommodation: '舒适型酒店',
  preferences: ['历史文化'],
  free_text_input: '',
  travel_style: '经典均衡',
  companions: '朋友',
  food_preferences: '',
  must_visit: '',
  avoid_places: '',
  low_intensity: false,
})

watch(
  () => [dateRange.start, dateRange.end],
  ([start, end]) => {
    if (!start || !end) return
    const days = end.diff(start, 'day') + 1
    if (days <= 0) {
      message.warning('结束日期不能早于开始日期')
      dateRange.end = null
      return
    }
    if (days > 30) {
      message.warning('旅行天数不能超过30天')
      dateRange.end = null
      return
    }
    formData.start_date = start.format('YYYY-MM-DD')
    formData.end_date = end.format('YYYY-MM-DD')
    formData.travel_days = days
  },
)

onMounted(async () => {
  try {
    serviceHealth.value = await healthCheck()
  } catch (error) {
    serviceHealth.value = {
      status: 'error',
      service: 'travel-assistant',
      llm: { enabled: false, model: 'unknown', base_url_configured: false, disabled: true },
      amap_configured: false,
      unsplash_configured: false,
      planner_mode: 'fast',
      cache_enabled: false,
      external_api_disabled: true,
      web_search: { enabled: false, tool: 'web_search' },
    }
  }
})

async function handleSubmit() {
  if (!dateRange.start || !dateRange.end) {
    message.error('请选择完整日期')
    return
  }

  loading.value = true
  loadingProgress.value = 5
  loadingStatus.value = '正在初始化多Agent协作...'
  const timer = window.setInterval(() => {
    if (loadingProgress.value < 90) {
      loadingProgress.value += 15
      if (loadingProgress.value <= 30) loadingStatus.value = '景点搜索专家正在搜索POI...'
      else if (loadingProgress.value <= 50) loadingStatus.value = '天气查询专家正在读取预报...'
      else if (loadingProgress.value <= 70) loadingStatus.value = '酒店推荐专家正在筛选住宿...'
      else loadingStatus.value = '行程规划专家正在整合计划...'
    }
  }, 450)

  try {
    const response = await generateTripPlan({ ...formData })
    window.clearInterval(timer)
    loadingProgress.value = 100
    loadingStatus.value = '规划完成'

    if (response.success && response.data) {
      const selected = response.data.options.find((option) => option.id === response.data!.selected_option_id)
      sessionStorage.setItem('tripPlanningResult', JSON.stringify(response.data))
      sessionStorage.setItem('tripPlan', JSON.stringify(selected?.plan || response.data.options[0]?.plan))
      message.success('旅行计划生成成功')
      window.setTimeout(() => router.push('/result'), 400)
    } else {
      message.error(response.message || '生成失败')
    }
  } catch (error: any) {
    window.clearInterval(timer)
    message.error(error.message || '生成旅行计划失败')
  } finally {
    window.setTimeout(() => {
      loading.value = false
      loadingProgress.value = 0
      loadingStatus.value = ''
    }, 800)
  }
}
</script>
