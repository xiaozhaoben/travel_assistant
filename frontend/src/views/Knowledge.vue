<template>
  <div class="knowledge-page">
    <section class="knowledge-header">
      <div>
        <p class="knowledge-eyebrow">RAG Knowledge Base</p>
        <h1>旅行知识库</h1>
        <p>上传文件或填写网页地址即可入库，标题、来源、省市、类型和发布时间会由大模型自动解析。</p>
      </div>
      <a-button :loading="newsIngesting" @click="handleIngestNews">
        <template #icon><SyncOutlined /></template>
        更新旅行资讯
      </a-button>
    </section>

    <main class="knowledge-shell">
      <section class="knowledge-ingest">
        <a-tabs v-model:active-key="activeTab">
          <a-tab-pane key="file" tab="上传文件">
            <div class="knowledge-dropzone">
              <input
                ref="fileInputRef"
                type="file"
                accept=".txt,.md,.markdown,.html,.htm,.json,.csv"
                @change="handleFileSelected"
              />
              <UploadOutlined />
              <div>
                <h2>{{ selectedFileName || '选择旅行资料文件' }}</h2>
                <p>支持 txt、Markdown、HTML、JSON、CSV。文件正文会提交给后端，由大模型识别 metadata 后写入向量库。</p>
              </div>
            </div>

            <div v-if="fileContent" class="knowledge-preview">
              {{ fileContent.slice(0, 520) }}
            </div>

            <div class="knowledge-actions">
              <a-button type="primary" :loading="fileSubmitting" :disabled="!fileContent" @click="submitFileDocument">
                <template #icon><UploadOutlined /></template>
                解析并入库
              </a-button>
            </div>
          </a-tab-pane>

          <a-tab-pane key="url" tab="网页地址">
            <label class="knowledge-url-field">
              <span>网页 URL</span>
              <a-input
                v-model:value="sourceUrl"
                placeholder="https://example.com/travel-policy"
                @press-enter="submitUrlDocument"
              />
            </label>

            <div class="knowledge-note">
              后端会抓取网页正文，再让大模型提取文档标题、来源、城市、省份、文档类型、发布时间和主题标签。
            </div>

            <div class="knowledge-actions">
              <a-button type="primary" :loading="urlSubmitting" :disabled="!sourceUrl.trim()" @click="submitUrlDocument">
                <template #icon><LinkOutlined /></template>
                抓取并入库
              </a-button>
            </div>
          </a-tab-pane>
        </a-tabs>

        <a-alert
          v-if="currentJob && currentJob.status !== 'completed'"
          class="knowledge-result-alert"
          :type="currentJob.status === 'failed' ? 'error' : 'info'"
          show-icon
          :message="`${formatJobStatus(currentJob.status)}：${currentJob.message}`"
          :description="currentJob.error || `job_id: ${currentJob.job_id}`"
        />

        <a-alert
          v-if="lastIngestResult"
          class="knowledge-result-alert"
          type="success"
          show-icon
          :message="`入库成功：${lastIngestResult.chunks_added} 个 chunk`"
          :description="`doc_id: ${lastIngestResult.doc_id}`"
        />
      </section>

      <aside class="knowledge-search">
        <div class="knowledge-search-header">
          <div>
            <h2>检索预览</h2>
            <p>用一个问题快速确认刚入库的资料能否被召回。</p>
          </div>
          <a-tag color="green">pgvector</a-tag>
        </div>

        <label class="knowledge-url-field">
          <span>问题</span>
          <a-textarea v-model:value="searchQuery" :rows="3" placeholder="成都有哪些适合亲子游的景点？" />
        </label>

        <div class="knowledge-search-controls">
          <label>
            <span>返回条数</span>
            <a-input-number v-model:value="topK" :min="1" :max="20" />
          </label>
          <a-button type="primary" :loading="searching" @click="submitSearch">
            <template #icon><SearchOutlined /></template>
            检索
          </a-button>
        </div>

        <div class="knowledge-results">
          <a-empty v-if="!searching && searchResults.length === 0" description="暂无检索结果" />
          <article v-for="item in searchResults" :key="item.chunk_id" class="knowledge-result-card">
            <div class="knowledge-result-title">
              <strong>{{ item.title }}</strong>
              <a-tag color="blue">{{ item.score.toFixed(3) }}</a-tag>
            </div>
            <p class="knowledge-result-section">{{ item.section }}</p>
            <p class="knowledge-result-content">{{ item.content }}</p>
            <footer>
              <span>{{ item.source_name }}</span>
              <a v-if="item.source_url" :href="item.source_url" target="_blank" rel="noreferrer">来源</a>
              <span v-if="item.publish_date">{{ item.publish_date }}</span>
            </footer>
          </article>
        </div>
      </aside>
    </main>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'
import { message } from 'ant-design-vue'
import { LinkOutlined, SearchOutlined, SyncOutlined, UploadOutlined } from '@ant-design/icons-vue'
import {
  createTravelDocumentAutoJob,
  createTravelDocumentUrlJob,
  getTravelDocumentJob,
  ingestTravelNews,
  searchTravelDocuments,
} from '@/services/api'
import type { TravelDocumentIngestJobStatus, TravelDocumentIngestResult, TravelDocumentSearchResult } from '@/types'

const activeTab = ref('file')
const fileInputRef = ref<HTMLInputElement | null>(null)
const selectedFileName = ref('')
const fileContent = ref('')
const sourceUrl = ref('')
const searchQuery = ref('')
const topK = ref(5)
const lastIngestResult = ref<TravelDocumentIngestResult | null>(null)
const currentJob = ref<TravelDocumentIngestJobStatus | null>(null)
const searchResults = ref<TravelDocumentSearchResult[]>([])
const fileSubmitting = ref(false)
const urlSubmitting = ref(false)
const searching = ref(false)
const newsIngesting = ref(false)
let jobPollTimer: number | undefined

async function handleFileSelected(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  try {
    selectedFileName.value = file.name
    fileContent.value = await file.text()
  } catch (error: any) {
    selectedFileName.value = ''
    fileContent.value = ''
    input.value = ''
    message.error(error.message || '文件读取失败')
  }
}

async function submitFileDocument() {
  if (!fileContent.value.trim()) {
    message.warning('请先选择一个可读取的文本文件')
    return
  }
  fileSubmitting.value = true
  try {
    const job = await createTravelDocumentAutoJob({
      content: fileContent.value,
      file_name: selectedFileName.value || undefined,
      source_type: 'upload',
    })
    startIngestJobPolling(job.job_id, 'file')
    message.success('文件解析任务已创建，后台正在入库')
    if (fileInputRef.value) fileInputRef.value.value = ''
  } catch (error: any) {
    message.error(error.message || '文件入库失败')
  } finally {
    fileSubmitting.value = false
  }
}

async function submitUrlDocument() {
  const value = sourceUrl.value.trim()
  if (!value) {
    message.warning('请输入网页 URL')
    return
  }
  urlSubmitting.value = true
  try {
    const job = await createTravelDocumentUrlJob({
      source_url: value,
    })
    startIngestJobPolling(job.job_id, 'url')
    message.success('网页抓取任务已创建，后台正在入库')
  } catch (error: any) {
    message.error(error.message || '网页入库失败')
  } finally {
    urlSubmitting.value = false
  }
}

async function submitSearch() {
  const query = searchQuery.value.trim()
  if (!query) {
    message.warning('请输入检索问题')
    return
  }
  searching.value = true
  try {
    const response = await searchTravelDocuments({ query, top_k: topK.value })
    searchResults.value = response.results
  } catch (error: any) {
    message.error(error.message || '检索失败')
  } finally {
    searching.value = false
  }
}

async function handleIngestNews() {
  newsIngesting.value = true
  try {
    const result = await ingestTravelNews()
    message.success(`已读取 ${result.total_seen} 条，新增 ${result.total_added} 个知识片段`)
  } catch (error: any) {
    message.error(error.message || '旅行资讯入库失败')
  } finally {
    newsIngesting.value = false
  }
}

function startIngestJobPolling(jobId: string, sourceType: 'file' | 'url') {
  clearIngestJobPolling()
  lastIngestResult.value = null
  const now = new Date().toISOString()
  currentJob.value = {
    job_id: jobId,
    status: 'queued',
    message: sourceType === 'file' ? '文件入库任务已创建' : '网页入库任务已创建',
    result: null,
    error: null,
    created_at: now,
    updated_at: now,
  }
  refreshIngestJob(jobId)
  jobPollTimer = window.setInterval(() => {
    refreshIngestJob(jobId)
  }, 1500)
}

async function refreshIngestJob(jobId: string) {
  try {
    const status = await getTravelDocumentJob(jobId)
    currentJob.value = status
    if (status.status === 'completed') {
      clearIngestJobPolling()
      lastIngestResult.value = status.result || null
      if (status.result) {
        message.success(`入库完成，生成 ${status.result.chunks_added} 个 chunk`)
      }
    } else if (status.status === 'failed') {
      clearIngestJobPolling()
      message.error(status.error || '入库任务失败')
    }
  } catch (error: any) {
    clearIngestJobPolling()
    message.error(error.message || '入库任务状态查询失败')
  }
}

function clearIngestJobPolling() {
  if (jobPollTimer !== undefined) {
    window.clearInterval(jobPollTimer)
    jobPollTimer = undefined
  }
}

function formatJobStatus(status: TravelDocumentIngestJobStatus['status']) {
  const labels = {
    queued: '排队中',
    running: '处理中',
    completed: '已完成',
    failed: '失败',
  }
  return labels[status]
}

onBeforeUnmount(clearIngestJobPolling)
</script>

<style scoped>
.knowledge-page {
  min-height: calc(100vh - 72px);
  background: var(--color-cream);
  color: var(--color-text-primary);
}

.knowledge-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: 24px;
  padding: 36px 48px 24px;
  background: var(--color-warm-white);
  border-bottom: 1px solid var(--color-border);
}

.knowledge-eyebrow {
  margin: 0 0 6px;
  font-size: 12px;
  font-weight: 700;
  color: var(--color-terracotta);
  text-transform: uppercase;
}

.knowledge-header h1 {
  margin: 0;
  font-size: 32px;
  font-family: var(--font-display);
  color: var(--color-forest-dark);
}

.knowledge-header p {
  margin: 8px 0 0;
  color: var(--color-text-secondary);
}

.knowledge-shell {
  display: grid;
  grid-template-columns: minmax(0, 1.05fr) minmax(360px, 0.95fr);
  gap: 24px;
  padding: 24px 48px 40px;
}

.knowledge-ingest,
.knowledge-search {
  background: #fff;
  border: 1px solid var(--color-border-light);
  border-radius: var(--radius-sm);
  padding: 20px;
  box-shadow: var(--shadow-sm);
}

.knowledge-dropzone {
  display: grid;
  grid-template-columns: minmax(190px, 240px) 42px minmax(0, 1fr);
  gap: 18px;
  align-items: center;
  padding: 20px;
  border: 1px dashed var(--color-border);
  border-radius: var(--radius-sm);
  background: var(--color-sand-light);
}

.knowledge-dropzone > span {
  font-size: 32px;
  color: var(--color-terracotta);
}

.knowledge-dropzone h2 {
  margin: 0 0 4px;
  font-size: 18px;
  color: var(--color-forest-dark);
}

.knowledge-dropzone p,
.knowledge-note,
.knowledge-search-header p {
  margin: 0;
  color: var(--color-text-secondary);
  line-height: 1.6;
}

.knowledge-preview,
.knowledge-note {
  margin-top: 16px;
  padding: 14px;
  border-radius: var(--radius-sm);
  background: var(--color-sand-light);
}

.knowledge-preview {
  max-height: 180px;
  overflow: auto;
  color: var(--color-text-secondary);
  white-space: pre-wrap;
  line-height: 1.65;
}

.knowledge-url-field {
  display: flex;
  flex-direction: column;
  gap: 8px;
  color: var(--color-text-secondary);
  font-size: 13px;
  font-weight: 600;
}

.knowledge-actions {
  display: flex;
  justify-content: flex-end;
  margin-top: 18px;
}

.knowledge-result-alert {
  margin-top: 18px;
}

.knowledge-search-header,
.knowledge-search-controls,
.knowledge-result-title,
.knowledge-result-card footer {
  display: flex;
  justify-content: space-between;
  gap: 14px;
  align-items: center;
}

.knowledge-search-header {
  align-items: flex-start;
  margin-bottom: 16px;
}

.knowledge-search-header h2 {
  margin: 0;
  font-size: 22px;
  color: var(--color-forest-dark);
}

.knowledge-search-controls {
  margin-top: 14px;
}

.knowledge-search-controls label {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--color-text-secondary);
}

.knowledge-results {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-top: 18px;
}

.knowledge-result-card {
  border: 1px solid var(--color-border-light);
  border-radius: var(--radius-sm);
  padding: 14px;
  background: var(--color-warm-white);
}

.knowledge-result-section {
  margin: 6px 0;
  color: var(--color-text-tertiary);
  font-size: 12px;
}

.knowledge-result-content {
  margin: 0;
  color: var(--color-text-secondary);
  line-height: 1.65;
  white-space: pre-wrap;
}

.knowledge-result-card footer {
  justify-content: flex-start;
  margin-top: 10px;
  color: var(--color-text-tertiary);
  font-size: 12px;
}

@media (max-width: 980px) {
  .knowledge-header {
    padding: 24px 20px;
    flex-direction: column;
    align-items: flex-start;
  }

  .knowledge-shell {
    grid-template-columns: 1fr;
    padding: 18px 20px 32px;
  }
}

@media (max-width: 640px) {
  .knowledge-dropzone,
  .knowledge-search-controls {
    grid-template-columns: 1fr;
    flex-direction: column;
    align-items: stretch;
  }
}
</style>
