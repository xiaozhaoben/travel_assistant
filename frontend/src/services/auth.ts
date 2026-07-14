import { computed, ref } from 'vue'
import type { AuthUser } from '@/types'
import { loginUser, mergeAnonymousSessions, registerUser } from '@/services/api'
import {
  clearMergedAnonymousToken,
  clearStoredUserPrincipal,
  ensureAnonymousToken,
  getPendingAnonymousTokens,
  getStoredUser,
  getStoredUserToken,
  hasPendingAnonymousMerge,
  persistUserPrincipal,
  stageCurrentAnonymousForMerge,
  subscribeToAuthSession,
} from '@/services/authSession'

export type AnonymousMergeState = 'not_needed' | 'merged' | 'pending'

const initialToken = getStoredUserToken()
const initialUser = initialToken ? getStoredUser() : null
const token = ref<string | null>(initialToken)
const user = ref<AuthUser | null>(initialUser)
const anonymousMergePending = ref(initialUser ? hasPendingAnonymousMerge(initialUser.user_id) : false)
const mergePromises = new Map<string, Promise<AnonymousMergeState>>()
const mergeRerunRequested = new Set<string>()

subscribeToAuthSession(() => {
  token.value = getStoredUserToken()
  user.value = getStoredUser()
  anonymousMergePending.value = user.value ? hasPendingAnonymousMerge(user.value.user_id) : false
})

export function useAuth() {
  const isAuthenticated = computed(() => Boolean(token.value && user.value))

  async function login(username: string, password: string): Promise<AuthUser> {
    const result = await loginUser(username, password)
    persistAuth(result.access_token, { user_id: result.user_id, username: result.username })
    stageCurrentAnonymousForMerge(result.user_id)
    await mergeAnonymousConversations(result.user_id)
    return user.value!
  }

  async function register(username: string, password: string): Promise<AuthUser> {
    const result = await registerUser(username, password)
    persistAuth(result.access_token, { user_id: result.user_id, username: result.username })
    stageCurrentAnonymousForMerge(result.user_id)
    await mergeAnonymousConversations(result.user_id)
    return user.value!
  }

  async function logout(): Promise<void> {
    token.value = null
    user.value = null
    clearStoredUserPrincipal()
    // 登出已经完成；后续受保护请求会安全重试匿名令牌签发。
    void ensureAnonymousToken().catch(() => undefined)
  }

  async function mergeAnonymousConversations(
    targetUserId = user.value?.user_id,
  ): Promise<AnonymousMergeState> {
    if (!targetUserId) return 'not_needed'
    const existingPromise = mergePromises.get(targetUserId)
    if (existingPromise) {
      mergeRerunRequested.add(targetUserId)
      return existingPromise
    }
    const storedUser = getStoredUser()
    const userToken = getStoredUserToken()
    const pendingTokens = getPendingAnonymousTokens(targetUserId)
    if (!userToken || storedUser?.user_id !== targetUserId || pendingTokens.length === 0) {
      if (user.value?.user_id === targetUserId) anonymousMergePending.value = pendingTokens.length > 0
      return pendingTokens.length > 0 ? 'pending' : 'not_needed'
    }

    const mergePromise = (async () => {
      let failed = false
      const attempted = new Set<string>()
      while (true) {
        const nextTokens = getPendingAnonymousTokens(targetUserId).filter((value) => !attempted.has(value))
        if (nextTokens.length === 0) break
        for (const anonymousToken of nextTokens) {
          attempted.add(anonymousToken)
          try {
            await mergeAnonymousSessions(anonymousToken, userToken)
            clearMergedAnonymousToken(targetUserId, anonymousToken)
          } catch {
            failed = true
          }
        }
      }
      const stillPending = hasPendingAnonymousMerge(targetUserId)
      if (user.value?.user_id === targetUserId) anonymousMergePending.value = stillPending
      return failed || stillPending ? 'pending' : 'merged'
    })().finally(() => {
      mergePromises.delete(targetUserId)
      const shouldRerun = mergeRerunRequested.delete(targetUserId)
      if (
        shouldRerun &&
        getStoredUser()?.user_id === targetUserId &&
        hasPendingAnonymousMerge(targetUserId)
      ) {
        queueMicrotask(() => void mergeAnonymousConversations(targetUserId))
      }
    })
    mergePromises.set(targetUserId, mergePromise)
    return mergePromise
  }

  return {
    token,
    user,
    isAuthenticated,
    anonymousMergePending,
    login,
    register,
    logout,
    mergeAnonymousConversations,
  }
}

function persistAuth(accessToken: string, authUser: AuthUser): void {
  persistUserPrincipal(accessToken, authUser)
}
