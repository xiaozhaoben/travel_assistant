import { computed, ref } from 'vue'
import type { AuthUser } from '@/types'
import { loginUser, mergeAnonymousSessions, registerUser } from '@/services/api'

const TOKEN_KEY = 'travel_auth_token'
const USER_KEY = 'travel_auth_user'
const ANON_KEY = 'travel_qa_anonymous_id'

function loadStoredUser(): AuthUser | null {
  try {
    const raw = localStorage.getItem(USER_KEY)
    if (!raw) return null
    return JSON.parse(raw) as AuthUser
  } catch {
    return null
  }
}

// 全局单例状态，确保多个组件共享同一份认证信息
const token = ref<string | null>(localStorage.getItem(TOKEN_KEY))
const user = ref<AuthUser | null>(loadStoredUser())

export function useAuth() {
  const isAuthenticated = computed(() => !!token.value && !!user.value)

  async function login(username: string, password: string): Promise<AuthUser> {
    const result = await loginUser(username, password)
    persistAuth(result.access_token, { user_id: result.user_id, username: result.username })
    await mergeAnonymousConversations()
    return user.value!
  }

  async function register(username: string, password: string): Promise<AuthUser> {
    const result = await registerUser(username, password)
    persistAuth(result.access_token, { user_id: result.user_id, username: result.username })
    await mergeAnonymousConversations()
    return user.value!
  }

  function logout(): void {
    token.value = null
    user.value = null
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(USER_KEY)
  }

  async function mergeAnonymousConversations(): Promise<void> {
    const anonymousId = localStorage.getItem(ANON_KEY)
    if (!anonymousId || !token.value) return
    try {
      await mergeAnonymousSessions(anonymousId)
      localStorage.removeItem(ANON_KEY)
    } catch {
      // 合并失败不影响登录流程
    }
  }

  return {
    token,
    user,
    isAuthenticated,
    login,
    register,
    logout,
    mergeAnonymousConversations,
  }
}

function persistAuth(accessToken: string, authUser: AuthUser): void {
  token.value = accessToken
  user.value = authUser
  localStorage.setItem(TOKEN_KEY, accessToken)
  localStorage.setItem(USER_KEY, JSON.stringify(authUser))
}
