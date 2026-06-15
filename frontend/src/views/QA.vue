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

      <div class="qa-identity">
        <a-input v-model:value="userIdInput" placeholder="输入用户标识后保存到个人历史" @press-enter="applyUserId" />
        <a-button @click="applyUserId">使用</a-button>
      </div>
      <div class="qa-identity-caption">
        当前：{{ activeUserId ? `用户 ${activeUserId}` : '访客会话' }}
      </div>

      <a-button block :loading="newsIngesting" class="qa-refresh-button" @click="handleIngestNews">
        <template #icon><SyncOutlined /></template>
        更新旅行资讯
      </a-button>

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
      <section class="qa-thread">
        <div v-if="messages.length === 0" class="qa-empty-state">
          <MessageOutlined />
          <h2>问我目的地、预约、交通、避坑和路线取舍</h2>
          <div class="qa-prompts">
            <button @click="usePrompt('端午去南京三天，有哪些预约和错峰建议？')">南京端午预约</button>
            <button @click="usePrompt('北京亲子三天，哪些博物馆适合提前安排？')">北京亲子博物馆</button>
            <button @click="usePrompt('去广州看历史文化和夜景，怎么避开太奔波？')">广州慢节奏</button>
          </div>
        </div>

        <div v-for="messageItem in messages" :key="messageItem.id" class="qa-message-row" :class="messageItem.role">
          <div class="qa-message-bubble">
            <div class="qa-message-content">{{ messageItem.content }}</div>
            <div v-if="messageItem.role === 'assistant'" class="qa-message-meta">
              <a-tag :color="generationModeColor(messageItem.generation_mode)">
                {{ generationModeText(messageItem.generation_mode) }}
              </a-tag>
              <a-tag v-if="messageItem.used_web_search" color="cyan">联网</a-tag>
              <span>{{ messageItem.retrieved_count }} 条资料</span>
            </div>
            <div v-if="messageItem.sources?.length" class="qa-sources">
              <a-tag v-for="source in messageItem.sources.slice(0, 4)" :key="source.title" color="blue">
                {{ source.title }}
              </a-tag>
            </div>
          </div>
        </div>
      </section>

      <section class="qa-composer">
        <a-textarea
          v-model:value="question"
          :rows="3"
          placeholder="继续追问，例如：那这些场馆分别怎么预约？"
          @keydown="handleComposerKeydown"
        />
        <div class="qa-composer-actions">
          <span>{{ activeConversationId ? '正在使用当前会话记忆' : '发送后会创建新会话' }}</span>
          <a-button type="primary" :loading="asking" @click="handleAsk">
            <template #icon><SendOutlined /></template>
            发送
          </a-button>
        </div>
      </section>
    </main>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import { MessageOutlined, PlusOutlined, SendOutlined, SyncOutlined } from '@ant-design/icons-vue'
import {
  getQAConversation,
  ingestTravelNews,
  listQAConversations,
  streamTravelQuestion,
} from '@/services/api'
import type { TravelQAChatMessage, TravelQAConversationSummary } from '@/types'

const ANON_KEY = 'travel_qa_anonymous_id'
const USER_KEY = 'travel_qa_user_id'

const conversations = ref<TravelQAConversationSummary[]>([])
const messages = ref<TravelQAChatMessage[]>([])
const activeConversationId = ref<string | null>(null)
const activeUserId = ref<string | null>(localStorage.getItem(USER_KEY))
const userIdInput = ref(activeUserId.value || '')
const anonymousId = ref(getAnonymousId())
const question = ref('')
const asking = ref(false)
const loadingConversations = ref(false)
const newsIngesting = ref(false)

onMounted(() => {
  refreshConversations()
})

function getAnonymousId(): string {
  const existing = localStorage.getItem(ANON_KEY)
  if (existing) return existing
  const next = `anon-${crypto.randomUUID()}`
  localStorage.setItem(ANON_KEY, next)
  return next
}

function identityParams() {
  return activeUserId.value
    ? { user_id: activeUserId.value, anonymous_id: null }
    : { anonymous_id: anonymousId.value, user_id: null }
}

async function refreshConversations() {
  loadingConversations.value = true
  try {
    conversations.value = await listQAConversations(identityParams())
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
  } catch (error: any) {
    message.error(error.message || '会话详情加载失败')
  }
}

function startNewConversation() {
  activeConversationId.value = null
  messages.value = []
  question.value = ''
}

function applyUserId() {
  const value = userIdInput.value.trim()
  activeUserId.value = value || null
  if (activeUserId.value) localStorage.setItem(USER_KEY, activeUserId.value)
  else localStorage.removeItem(USER_KEY)
  startNewConversation()
  refreshConversations()
}

function usePrompt(value: string) {
  question.value = value
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
      ...identityParams(),
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
  min-height: calc(100vh - 72px);
  display: grid;
  grid-template-columns: 340px minmax(0, 1fr);
  background: var(--color-cream);
}

.qa-sidebar {
  border-right: 1px solid var(--color-border);
  background: var(--color-warm-white);
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.qa-sidebar-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.qa-sidebar-header h1 {
  margin: 0;
  font-size: 24px;
  font-family: var(--font-display);
  color: var(--color-forest-dark);
}

.qa-sidebar-header p,
.qa-identity-caption {
  margin: 4px 0 0;
  color: var(--color-text-secondary);
  font-size: 13px;
}

.qa-identity {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
}

.qa-refresh-button {
  border-color: var(--color-border);
}

.qa-conversation-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  overflow: auto;
}

.qa-conversation-item {
  width: 100%;
  text-align: left;
  border: 1px solid var(--color-border-light);
  background: #fff;
  border-radius: var(--radius-sm);
  padding: 12px;
  cursor: pointer;
  transition: all var(--transition-fast);
}

.qa-conversation-item span {
  display: block;
  color: var(--color-text-primary);
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.qa-conversation-item small {
  display: block;
  margin-top: 4px;
  color: var(--color-text-tertiary);
}

.qa-conversation-item.active,
.qa-conversation-item:hover {
  border-color: var(--color-forest-light);
  background: var(--color-sand-light);
}

.qa-main {
  min-width: 0;
  display: grid;
  grid-template-rows: minmax(0, 1fr) auto;
}

.qa-thread {
  padding: 32px;
  overflow: auto;
}

.qa-empty-state {
  max-width: 760px;
  margin: 12vh auto 0;
  text-align: center;
  color: var(--color-forest-dark);
}

.qa-empty-state > span {
  font-size: 44px;
  color: var(--color-terracotta);
}

.qa-empty-state h2 {
  margin: 16px 0 24px;
  font-size: 28px;
  font-family: var(--font-display);
}

.qa-prompts {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}

.qa-prompts button {
  border: 1px solid var(--color-border);
  background: #fff;
  border-radius: var(--radius-sm);
  padding: 14px;
  cursor: pointer;
  color: var(--color-forest-dark);
}

.qa-message-row {
  display: flex;
  margin-bottom: 18px;
}

.qa-message-row.user {
  justify-content: flex-end;
}

.qa-message-bubble {
  max-width: min(760px, 82%);
  border-radius: var(--radius-sm);
  padding: 14px 16px;
  background: #fff;
  border: 1px solid var(--color-border-light);
  box-shadow: var(--shadow-sm);
}

.qa-message-row.user .qa-message-bubble {
  background: var(--color-forest);
  color: #fff;
}

.qa-message-content {
  white-space: pre-wrap;
  line-height: 1.7;
  word-break: break-word;
}

.qa-message-meta,
.qa-sources {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 10px;
  align-items: center;
  color: var(--color-text-tertiary);
  font-size: 12px;
}

.qa-composer {
  border-top: 1px solid var(--color-border);
  padding: 18px 32px 24px;
  background: var(--color-warm-white);
}

.qa-composer-actions {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  margin-top: 12px;
  color: var(--color-text-secondary);
  font-size: 13px;
}

@media (max-width: 900px) {
  .qa-page {
    grid-template-columns: 1fr;
  }

  .qa-sidebar {
    border-right: none;
    border-bottom: 1px solid var(--color-border);
  }

  .qa-prompts {
    grid-template-columns: 1fr;
  }

  .qa-thread,
  .qa-composer {
    padding-left: 18px;
    padding-right: 18px;
  }
}
</style>
