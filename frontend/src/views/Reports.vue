<template>
  <div class="reports-container">
    <div class="reports-header">
      <div>
        <h1>历史报表</h1>
        <p>查看已生成的旅行规划记录，继续打开行程详情。</p>
      </div>
      <a-space>
        <a-button @click="router.push('/')">
          <template #icon><ArrowLeftOutlined /></template>
          返回首页
        </a-button>
        <a-button type="primary" :loading="loading" @click="loadReports">
          <template #icon><ReloadOutlined /></template>
          刷新
        </a-button>
      </a-space>
    </div>

    <a-card :bordered="false" class="reports-table-card">
      <a-table
        row-key="id"
        :columns="columns"
        :data-source="reports"
        :loading="loading"
        :pagination="{ pageSize: 12, showSizeChanger: false }"
        :scroll="{ x: 960 }"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'prompt'">
            <div class="report-prompt">{{ record.prompt }}</div>
          </template>
          <template v-else-if="column.key === 'budget_total'">¥{{ record.budget_total }}</template>
          <template v-else-if="column.key === 'generation_mode'">
            <a-tag :color="record.generation_mode === 'llm' ? 'green' : 'orange'">
              {{ record.generation_mode === 'llm' ? '大模型' : 'Fallback' }}
            </a-tag>
          </template>
          <template v-else-if="column.key === 'created_at'">{{ formatDate(record.created_at) }}</template>
          <template v-else-if="column.key === 'action'">
            <a-button type="link" :loading="openingId === record.id" @click="openReport(record.id)">
              打开行程
            </a-button>
          </template>
        </template>
      </a-table>
    </a-card>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { ArrowLeftOutlined, ReloadOutlined } from '@ant-design/icons-vue'
import dayjs from 'dayjs'
import { getTripReport, listTripReports, normalizeReportPlanningResult } from '@/services/api'
import type { TripReportSummary } from '@/types'

const router = useRouter()
const reports = ref<TripReportSummary[]>([])
const loading = ref(false)
const openingId = ref('')

const columns = [
  { title: '需求', dataIndex: 'prompt', key: 'prompt', width: 360 },
  { title: '城市', dataIndex: 'city', key: 'city', width: 120 },
  { title: '天数', dataIndex: 'days_count', key: 'days_count', width: 90 },
  { title: '预算', dataIndex: 'budget_total', key: 'budget_total', width: 110 },
  { title: '模式', dataIndex: 'generation_mode', key: 'generation_mode', width: 120 },
  { title: '创建时间', dataIndex: 'created_at', key: 'created_at', width: 180 },
  { title: '操作', key: 'action', fixed: 'right', width: 120 },
]

onMounted(loadReports)

async function loadReports() {
  loading.value = true
  try {
    reports.value = await listTripReports()
  } catch (error: any) {
    message.error(error.message || '历史报表获取失败')
  } finally {
    loading.value = false
  }
}

async function openReport(reportId: string) {
  openingId.value = reportId
  try {
    const detail = await getTripReport(reportId)
    const planningResult = normalizeReportPlanningResult(detail)
    const selected = planningResult.options.find((option) => option.id === planningResult.selected_option_id)
    sessionStorage.setItem('tripPlanningResult', JSON.stringify(planningResult))
    sessionStorage.setItem('tripPlan', JSON.stringify(selected?.plan || planningResult.options[0]?.plan))
    await router.push('/result')
  } catch (error: any) {
    message.error(error.message || '历史报表打开失败')
  } finally {
    openingId.value = ''
  }
}

function formatDate(value: string) {
  return dayjs(value).format('YYYY-MM-DD HH:mm')
}
</script>
