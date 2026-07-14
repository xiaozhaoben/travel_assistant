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
} from '@/services/authSession'

export type AnonymousMergeState = 'not_needed' | 'merged' | 'pending'

const token = ref<string | null>(getStoredUserToken())
const user = ref<AuthUser | null>(getStoredUser())
const anonymousMergePending = ref(hasPendingAnonymousMerge())
let mergePromise: Promise<AnonymousMergeState> | null = null

export function useAuth() {
  const isAuthenticated = computed(() => Boolean(token.value && user.value))

  async function login(username: string, password: string): Promise<AuthUser> {
    const result = await loginUser(username, password)
    persistAuth(result.access_token, { user_id: result.user_id, username: result.username })
    stageCurrentAnonymousForMerge()
    await mergeAnonymousConversations()
    return user.value!
  }

  async function register(username: string, password: string): Promise<AuthUser> {
    const result = await registerUser(username, password)
    persistAuth(result.access_token, { user_id: result.user_id, username: result.username })
    stageCurrentAnonymousForMerge()
    await mergeAnonymousConversations()
    return user.value!
  }

  async function logout(): Promise<void> {
    token.value = null
    user.value = null
    clearStoredUserPrincipal()
    await ensureAnonymousToken()
  }

  async function mergeAnonymousConversations(): Promise<AnonymousMergeState> {
    if (mergePromise) return mergePromise
    const userToken = getStoredUserToken()
    const pendingTokens = getPendingAnonymousTokens()
    if (!userToken || pendingTokens.length === 0) {
      anonymousMergePending.value = pendingTokens.length > 0
      return pendingTokens.length > 0 ? 'pending' : 'not_needed'
    }

    mergePromise = (async () => {
      let failed = false
      for (const anonymousToken of pendingTokens) {
        try {
          await mergeAnonymousSessions(anonymousToken, userToken)
          clearMergedAnonymousToken(anonymousToken)
        } catch {
          failed = true
        }
      }
      anonymousMergePending.value = hasPendingAnonymousMerge()
      return failed || anonymousMergePending.value ? 'pending' : 'merged'
    })().finally(() => {
      mergePromise = null
    })
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
  token.value = accessToken
  user.value = authUser
  persistUserPrincipal(accessToken, authUser)
}
