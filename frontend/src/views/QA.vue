<template>
  <div class="qa-page">
    <aside class="qa-sidebar">
      <div class="qa-sidebar-header">
        <div>
          <h1>旅行智能问答</h1>
          <p>多轮记忆 · 实时搜索 · 旅行知识库</p>
        </div>
        <a-button type="primary" @click="startNewConversation">
          <template #icon><PlusOutlined /></template>
          新对话
        </a-button>
      </div>

      <div class="qa-identity-caption">
        <template v-if="auth.isAuthenticated.value">
          已登录为 <strong>{{ auth.user.value?.username }}</strong>
        </template>
        <template v-else>
          访客会话 · <router-link to="/login">登录同步历史</router-link>
        </template>
      </div>

      <div class="qa-conversation-list">
        <a-empty v-if="!loadingConversations && conversations.length === 0" description="暂无历史对话" />
        <button
          v-for="conversation in conversations"
          :key="conversation.id"
          class="qa-conversation-item"
          :class="{ active: conversation.id === activeConversationId }"
          @click="loadConversation(conversation.id)"
        >
          <span>{{ conversation.title }}</span>
          <small>{{ formatDate(conversation.updated_at) }}</small>
        </button>
      </div>
    </aside>

    <main class="qa-main">
      <section ref="threadRef" class="qa-thread">
        <div v-if="messages.length === 0" class="qa-empty-state">
          <div class="qa-empty-icon">
            <MessageOutlined />
          </div>
          <h2>问我目的地、预约、交通、避坑和路线取舍</h2>
          <p class="qa-empty-hint">我会从知识库和实时搜索中为你找到最佳答案</p>
          <div class="qa-prompts">
            <button @click="usePrompt('端午去南京三天，有哪些预约和错峰建议？')">
              <span class="qa-prompt-icon">🏛</span>
              <span>南京端午预约</span>
            </button>
            <button @click="usePrompt('北京亲子三天，哪些博物馆适合提前安排？')">
              <span class="qa-prompt-icon">🎨</span>
              <span>北京亲子博物馆</span>
            </button>
            <button @click="usePrompt('去广州看历史文化和夜景，怎么避开太奔波？')">
              <span class="qa-prompt-icon">🌃</span>
              <span>广州慢节奏</span>
            </button>
          </div>
        </div>

        <div v-for="messageItem in messages" :key="messageItem.id" class="qa-message-row" :class="messageItem.role">
          <div class="qa-message-bubble">
            <template v-if="messageItem.role === 'assistant' && extractSummary(messageItem.content).summary">
              <a-collapse :bordered="false" class="qa-summary-collapse">
                <a-collapse-panel key="summary" header="对话摘要">
                  <div class="qa-summary-content">{{ extractSummary(messageItem.content).summary }}</div>
                </a-collapse-panel>
              </a-collapse>
              <div class="qa-message-content" v-html="linkifyContent(extractSummary(messageItem.content).main)"></div>
            </template>
            <div v-else class="qa-message-content" v-html="linkifyContent(messageItem.content)"></div>
            <div v-if="messageItem.role === 'assistant'" class="qa-message-meta">
              <a-tag :color="generationModeColor(messageItem.generation_mode)">
                {{ generationModeText(messageItem.generation_mode) }}
              </a-tag>
              <a-tag v-if="messageItem.used_web_search" color="cyan">联网</a-tag>
              <span>{{ messageItem.retrieved_count }} 条资料</span>
            </div>
            <div v-if="messageItem.sources?.length" class="qa-sources">
              <template v-for="source in messageItem.sources.slice(0, 4)" :key="source.title">
                <a
                  v-if="source.url"
                  :href="source.url"
                  target="_blank"
                  rel="noopener noreferrer"
                  class="qa-source-link"
                >
                  <a-tag color="blue">{{ source.title }}</a-tag>
                </a>
                <a-tag v-else color="blue">{{ source.title }}</a-tag>
              </template>
            </div>
          </div>
        </div>
      </section>

      <section class="qa-composer">
        <div class="qa-composer-inner">
          <a-textarea
            v-model:value="question"
            :rows="3"
            placeholder="继续追问，例如：那这些场馆分别怎么预约？"
            @keydown="handleComposerKeydown"
          />
          <div class="qa-composer-actions">
            <span class="qa-composer-hint">{{ activeConversationId ? '正在使用当前会话记忆' : '发送后会创建新会话' }}</span>
            <a-button type="primary" :loading="asking" @click="handleAsk">
              <template #icon><SendOutlined /></template>
              发送
            </a-button>
          </div>
        </div>
      </section>
    </main>
  </div>
</template>

<script setup lang="ts">
import { nextTick, onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import { MessageOutlined, PlusOutlined, SendOutlined } from '@ant-design/icons-vue'
import {
  getQAConversation,
  ingestTravelNews,
  listQAConversations,
  streamTravelQuestion,
} from '@/services/api'
import { useAuth } from '@/services/auth'
import type { TravelQAChatMessage, TravelQAConversationSummary } from '@/types'

const auth = useAuth()
const conversations = ref<TravelQAConversationSummary[]>([])
const messages = ref<TravelQAChatMessage[]>([])
const activeConversationId = ref<string | null>(null)
const question = ref('')
const asking = ref(false)
const loadingConversations = ref(false)
const newsIngesting = ref(false)
const threadRef = ref<HTMLElement | null>(null)

onMounted(() => {
  refreshConversations()
})

async function refreshConversations() {
  loadingConversations.value = true
  try {
    conversations.value = await listQAConversations()
  } catch (error: any) {
    message.error(error.message || '问答历史加载失败')
  } finally {
    loadingConversations.value = false
  }
}

async function loadConversation(conversationId: string) {
  try {
    const detail = await getQAConversation(conversationId)
    activeConversationId.value = detail.id
    messages.value = detail.messages
    await nextTick()
    threadRef.value?.scrollTo({ top: threadRef.value.scrollHeight, behavior: 'smooth' })
  } catch (error: any) {
    message.error(error.message || '会话详情加载失败')
  }
}

function startNewConversation() {
  activeConversationId.value = null
  messages.value = []
  question.value = ''
}

function usePrompt(value: string) {
  question.value = value
}

const SUMMARY_PREFIX = '**Summary of the conversation so far'

function extractSummary(content: string): { summary: string | null; main: string } {
  if (!content.startsWith(SUMMARY_PREFIX)) {
    return { summary: null, main: content }
  }
  const endMarkers = ['\n\n\n', '\n\n> ', '\n\n---\n']
  let splitAt = -1
  for (const marker of endMarkers) {
    const idx = content.indexOf(marker, SUMMARY_PREFIX.length)
    if (idx !== -1 && (splitAt === -1 || idx < splitAt)) splitAt = idx
  }
  if (splitAt === -1) {
    return { summary: content.replace(/^\*\*/, '').replace(/\*\*$/, '').trim(), main: '' }
  }
  const rawSummary = content.slice(0, splitAt).replace(/^\*\*/, '').replace(/\*\*$/, '').trim()
  const main = content.slice(splitAt).trim()
  return { summary: rawSummary || null, main: main || content }
}

function generationModeText(mode: TravelQAChatMessage['generation_mode']): string {
  if (mode === 'llm') return '大模型'
  if (mode === 'fallback') return 'Fallback'
  return '生成中'
}

function generationModeColor(mode: TravelQAChatMessage['generation_mode']): string {
  if (mode === 'llm') return 'green'
  if (mode === 'fallback') return 'orange'
  return 'blue'
}

function handleComposerKeydown(event: KeyboardEvent) {
  if (event.key !== 'Enter' || event.isComposing) return
  if (event.ctrlKey || event.metaKey) {
    const target = event.target as HTMLTextAreaElement | null
    if (!target) return
    event.preventDefault()
    const start = target.selectionStart ?? question.value.length
    const end = target.selectionEnd ?? question.value.length
    question.value = `${question.value.slice(0, start)}\n${question.value.slice(end)}`
    requestAnimationFrame(() => {
      target.selectionStart = start + 1
      target.selectionEnd = start + 1
    })
    return
  }
  event.preventDefault()
  handleAsk()
}

async function handleAsk() {
  const content = question.value.trim()
  if (content.length < 2) {
    message.warning('请输入旅行相关问题')
    return
  }
  const pendingMessage: TravelQAChatMessage = {
    id: `pending-${Date.now()}`,
    conversation_id: activeConversationId.value || '',
    role: 'user',
    content,
    sources: [],
    retrieved_count: 0,
    used_web_search: false,
    created_at: new Date().toISOString(),
  }
  const assistantMessage: TravelQAChatMessage = {
    id: `answer-${Date.now()}`,
    conversation_id: activeConversationId.value || '',
    role: 'assistant',
    content: '',
    sources: [],
    retrieved_count: 0,
    generation_mode: null,
    used_web_search: false,
    created_at: new Date().toISOString(),
  }
  messages.value = [...messages.value, pendingMessage]
  question.value = ''
  asking.value = true
  try {
    messages.value = [...messages.value, assistantMessage]
    await streamTravelQuestion(content, {
      topK: 5,
      conversation_id: activeConversationId.value,
      onStart: (data) => {
        if (data.conversation_id) {
          activeConversationId.value = data.conversation_id
          assistantMessage.conversation_id = data.conversation_id
          pendingMessage.conversation_id = data.conversation_id
        }
      },
      onDelta: (delta) => {
        assistantMessage.content += delta
        messages.value = [...messages.value]
      },
      onDone: (answer) => {
        if (answer.conversation_id) activeConversationId.value = answer.conversation_id
        assistantMessage.id = answer.message_id || assistantMessage.id
        assistantMessage.conversation_id = answer.conversation_id || activeConversationId.value || ''
        assistantMessage.content = answer.answer
        assistantMessage.sources = answer.sources
        assistantMessage.retrieved_count = answer.retrieved_count
        assistantMessage.generation_mode = answer.generation_mode
        assistantMessage.used_web_search = answer.used_web_search
        messages.value = [...messages.value]
      },
    })
    refreshConversations()
  } catch (error: any) {
    messages.value = messages.value.filter((item) => item.id !== pendingMessage.id && item.id !== assistantMessage.id)
    message.error(error.message || '智能问答失败')
  } finally {
    asking.value = false
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

function linkifyContent(text: string): string {
  if (!text) return ''
  const escaped = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
  return escaped.replace(
    /(https?:\/\/[^\s<>&"']+)/g,
    '<a href="$1" target="_blank" rel="noopener noreferrer" class="qa-inline-link">$1</a>',
  )
}

function formatDate(value: string): string {
  return new Date(value).toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}
</script>

<style scoped>
.qa-page {
  height: calc(100vh - 64px);
  display: grid;
  grid-template-columns: 320px minmax(0, 1fr);
  background: var(--surface);
  overflow: hidden;
}

/* ─── Sidebar ──────────────────────────────────────────────── */
.qa-sidebar {
  border-right: 1px solid var(--border);
  background: var(--card);
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
  height: 100%;
  overflow-y: auto;
}

.qa-sidebar-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}
.qa-sidebar-header h1 {
  margin: 0;
  font-size: 20px;
  font-family: var(--font-display);
  color: var(--text-primary);
  font-weight: 800;
  letter-spacing: -0.01em;
}
.qa-sidebar-header p {
  margin: 4px 0 0;
  color: var(--text-muted);
  font-size: 12px;
}

.qa-identity-caption {
  color: var(--text-muted);
  font-size: 12px;
  padding: 8px 12px;
  background: var(--surface);
  border-radius: var(--radius-md);
}
.qa-identity-caption a {
  color: var(--accent);
  text-decoration: none;
  font-weight: 500;
}
.qa-identity-caption a:hover { text-decoration: underline; }

.qa-conversation-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  overflow: auto;
  flex: 1;
}

.qa-conversation-item {
  width: 100%;
  text-align: left;
  border: 1px solid transparent;
  background: transparent;
  border-radius: var(--radius-lg);
  padding: 12px 14px;
  cursor: pointer;
  transition: all var(--transition-fast);
  font-family: var(--font-body);
}
.qa-conversation-item:hover {
  background: var(--surface);
  border-color: var(--border-light);
}
.qa-conversation-item span {
  display: block;
  color: var(--text-primary);
  font-weight: 600;
  font-size: 13.5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.qa-conversation-item small {
  display: block;
  margin-top: 4px;
  color: var(--text-muted);
  font-size: 11.5px;
}
.qa-conversation-item.active {
  background: var(--accent-bg);
  border-color: rgba(249, 115, 22, 0.2);
}
.qa-conversation-item.active span {
  color: var(--accent-hover);
}

/* ─── Main Area ────────────────────────────────────────────── */
.qa-main {
  min-width: 0;
  display: grid;
  grid-template-rows: minmax(0, 1fr) auto;
  height: 100%;
  overflow: hidden;
  background: var(--surface);
}

.qa-thread {
  padding: 32px;
  overflow: auto;
}

/* ─── Empty State ──────────────────────────────────────────── */
.qa-empty-state {
  max-width: 640px;
  margin: 10vh auto 0;
  text-align: center;
}
.qa-empty-icon {
  width: 72px;
  height: 72px;
  margin: 0 auto 20px;
  border-radius: var(--radius-2xl);
  background: linear-gradient(135deg, var(--accent-bg), #FEF3C7);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 32px;
  color: var(--accent);
}
.qa-empty-state h2 {
  margin: 0 0 8px;
  font-size: 24px;
  font-family: var(--font-display);
  color: var(--text-primary);
  font-weight: 700;
}
.qa-empty-hint {
  margin: 0 0 28px;
  color: var(--text-muted);
  font-size: 14px;
}
.qa-prompts {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}
.qa-prompts button {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  border: 1px solid var(--border);
  background: var(--card);
  border-radius: var(--radius-xl);
  padding: 18px 14px;
  cursor: pointer;
  color: var(--text-primary);
  font-family: var(--font-body);
  font-size: 13.5px;
  font-weight: 500;
  transition: all var(--transition-base);
  box-shadow: var(--shadow-xs);
}
.qa-prompts button:hover {
  border-color: var(--accent);
  box-shadow: var(--shadow-md);
  transform: translateY(-2px);
}
.qa-prompt-icon {
  font-size: 24px;
  line-height: 1;
}

/* ─── Messages ─────────────────────────────────────────────── */
.qa-message-row {
  display: flex;
  margin-bottom: 16px;
  animation: fadeInUp 0.3s ease-out;
}
.qa-message-row.user {
  justify-content: flex-end;
}

.qa-message-bubble {
  max-width: min(720px, 80%);
  border-radius: var(--radius-xl);
  padding: 14px 18px;
  background: var(--card);
  border: 1px solid var(--border-light);
  box-shadow: var(--shadow-xs);
}
.qa-message-row.user .qa-message-bubble {
  background: linear-gradient(135deg, var(--primary), var(--primary-light));
  color: #fff;
  border: none;
}
.qa-message-row.user .qa-message-content {
  color: rgba(255, 255, 255, 0.95);
}

.qa-message-content {
  white-space: pre-wrap;
  line-height: 1.75;
  word-break: break-word;
  font-size: 14px;
}
.qa-message-content :deep(.qa-inline-link) {
  color: var(--secondary);
  text-decoration: underline;
  word-break: break-all;
}
.qa-message-content :deep(.qa-inline-link:hover) {
  color: var(--accent);
}

.qa-source-link {
  text-decoration: none;
  cursor: pointer;
}
.qa-source-link:hover :deep(.ant-tag) { opacity: 0.8; }

.qa-summary-collapse {
  margin-bottom: 10px;
  border-radius: var(--radius-md) !important;
  background: var(--surface) !important;
  font-size: 12.5px;
}
.qa-summary-collapse :deep(.ant-collapse-header) {
  padding: 6px 12px !important;
  font-weight: 600;
  color: var(--text-primary);
}
.qa-summary-content {
  white-space: pre-wrap;
  line-height: 1.6;
  font-size: 12px;
  color: var(--text-secondary);
}

.qa-message-meta,
.qa-sources {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 10px;
  align-items: center;
  color: var(--text-muted);
  font-size: 11.5px;
}

/* ─── Composer ─────────────────────────────────────────────── */
.qa-composer {
  border-top: 1px solid var(--border);
  padding: 16px 32px 24px;
  background: var(--card);
}
.qa-composer-inner {
  max-width: 760px;
  margin: 0 auto;
}
.qa-composer :deep(.ant-input) {
  border-radius: var(--radius-xl) !important;
  padding: 14px 18px !important;
  font-size: 14px !important;
  resize: none;
  box-shadow: var(--shadow-sm);
  border-color: var(--border) !important;
}
.qa-composer :deep(.ant-input:focus),
.qa-composer :deep(.ant-input-focused) {
  border-color: var(--accent) !important;
  box-shadow: 0 0 0 3px rgba(249, 115, 22, 0.1), var(--shadow-sm) !important;
}

.qa-composer-actions {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  margin-top: 10px;
}
.qa-composer-hint {
  color: var(--text-muted);
  font-size: 12px;
}

@media (max-width: 900px) {
  .qa-page {
    grid-template-columns: 1fr;
  }
  .qa-sidebar {
    border-right: none;
    border-bottom: 1px solid var(--border);
    max-height: 200px;
  }
  .qa-prompts {
    grid-template-columns: 1fr;
  }
  .qa-thread,
  .qa-composer {
    padding-left: 16px;
    padding-right: 16px;
  }
}
</style>
