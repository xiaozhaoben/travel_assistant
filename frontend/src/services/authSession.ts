import type { AuthUser, PrincipalTokenResponse } from '@/types'

const USER_TOKEN_KEY = 'travel_auth_token'
const USER_KEY = 'travel_auth_user'
const ANONYMOUS_PRINCIPAL_KEY = 'travel_anonymous_principal'
const PENDING_ANONYMOUS_MERGES_PREFIX = 'travel_pending_anonymous_merges_by_user'
const LEGACY_PENDING_ANONYMOUS_MERGES_KEY = 'travel_pending_anonymous_merges'

interface StoredAnonymousPrincipal {
  access_token: string
  subject: string
  expires_at: number
}

let anonymousTokenIssuer: (() => Promise<PrincipalTokenResponse>) | null = null
let anonymousIssuePromise: Promise<string> | null = null
const authSessionListeners = new Set<() => void>()

// 旧版本没有记录目标用户，无法安全判断归属，因此不能迁移。
localStorage.removeItem(LEGACY_PENDING_ANONYMOUS_MERGES_KEY)

export function subscribeToAuthSession(listener: () => void): () => void {
  authSessionListeners.add(listener)
  return () => authSessionListeners.delete(listener)
}

function notifyAuthSessionChanged(): void {
  authSessionListeners.forEach((listener) => listener())
}

export function configureAnonymousTokenIssuer(issuer: () => Promise<PrincipalTokenResponse>): void {
  anonymousTokenIssuer = issuer
}

export function getStoredUserToken(): string | null {
  const accessToken = localStorage.getItem(USER_TOKEN_KEY)
  if (!accessToken) return null
  if (tokenExpiresSoon(accessToken)) {
    clearStoredUserPrincipal()
    return null
  }
  return accessToken
}

export function getStoredUser(): AuthUser | null {
  try {
    const raw = localStorage.getItem(USER_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as Partial<AuthUser>
    if (typeof parsed.user_id !== 'string' || typeof parsed.username !== 'string') return null
    return { user_id: parsed.user_id, username: parsed.username }
  } catch {
    return null
  }
}

export function hasStoredUserPrincipal(): boolean {
  return Boolean(getStoredUserToken() && getStoredUser())
}

export function persistUserPrincipal(accessToken: string, authUser: AuthUser): void {
  localStorage.setItem(USER_TOKEN_KEY, accessToken)
  localStorage.setItem(USER_KEY, JSON.stringify(authUser))
  notifyAuthSessionChanged()
}

export function clearStoredUserPrincipal(): void {
  const changed = Boolean(localStorage.getItem(USER_TOKEN_KEY) || localStorage.getItem(USER_KEY))
  localStorage.removeItem(USER_TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
  if (changed) notifyAuthSessionChanged()
}

export function getStoredAnonymousToken(): string | null {
  const principal = readAnonymousPrincipal(ANONYMOUS_PRINCIPAL_KEY)
  if (!principal) return null
  if (principal.expires_at <= Date.now() + 30_000) {
    localStorage.removeItem(ANONYMOUS_PRINCIPAL_KEY)
    return null
  }
  return principal.access_token
}

export function clearAnonymousPrincipal(): void {
  const changed = localStorage.getItem(ANONYMOUS_PRINCIPAL_KEY) !== null
  localStorage.removeItem(ANONYMOUS_PRINCIPAL_KEY)
  if (changed) notifyAuthSessionChanged()
}

export function stageCurrentAnonymousForMerge(targetUserId: string): void {
  const principal = readAnonymousPrincipal(ANONYMOUS_PRINCIPAL_KEY)
  if (!principal) return
  const pending = readPendingAnonymousPrincipals(targetUserId)
  if (!pending.some((item) => item.access_token === principal.access_token)) {
    pending.push(principal)
    writePendingAnonymousPrincipals(targetUserId, pending)
  }
  clearAnonymousPrincipal()
}

export function getPendingAnonymousTokens(targetUserId: string): string[] {
  return readPendingAnonymousPrincipals(targetUserId).map((principal) => principal.access_token)
}

export function clearMergedAnonymousToken(targetUserId: string, accessToken: string): void {
  writePendingAnonymousPrincipals(
    targetUserId,
    readPendingAnonymousPrincipals(targetUserId).filter((principal) => principal.access_token !== accessToken),
  )
}

export function hasPendingAnonymousMerge(targetUserId: string): boolean {
  return readPendingAnonymousPrincipals(targetUserId).length > 0
}

export async function ensureAnonymousToken(): Promise<string> {
  const userToken = getStoredUserToken()
  if (userToken && getStoredUser()) return userToken

  const storedToken = getStoredAnonymousToken()
  if (storedToken) return storedToken
  if (anonymousIssuePromise) return anonymousIssuePromise
  if (!anonymousTokenIssuer) throw new Error('Anonymous token issuer is not configured')

  anonymousIssuePromise = anonymousTokenIssuer()
    .then((principal) => {
      if (principal.principal_type !== 'anonymous' || !principal.access_token || !principal.subject) {
        throw new Error('Anonymous token response is invalid')
      }
      const stored: StoredAnonymousPrincipal = {
        access_token: principal.access_token,
        subject: principal.subject,
        expires_at: Date.now() + principal.expires_in * 1000,
      }
      localStorage.setItem(ANONYMOUS_PRINCIPAL_KEY, JSON.stringify(stored))
      notifyAuthSessionChanged()
      return principal.access_token
    })
    .finally(() => {
      anonymousIssuePromise = null
    })
  return anonymousIssuePromise
}

export async function resolveBearerToken(): Promise<string> {
  const userToken = getStoredUserToken()
  if (userToken && getStoredUser()) return userToken
  return ensureAnonymousToken()
}

export function invalidateBearerToken(accessToken: string): void {
  if (localStorage.getItem(USER_TOKEN_KEY) === accessToken) {
    clearStoredUserPrincipal()
    return
  }
  if (getStoredAnonymousToken() === accessToken) clearAnonymousPrincipal()
}

function readAnonymousPrincipal(key: string): StoredAnonymousPrincipal | null {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return null
    const parsed = JSON.parse(raw) as Partial<StoredAnonymousPrincipal>
    if (
      typeof parsed.access_token !== 'string' ||
      typeof parsed.subject !== 'string' ||
      typeof parsed.expires_at !== 'number'
    ) {
      return null
    }
    return parsed as StoredAnonymousPrincipal
  } catch {
    return null
  }
}

function pendingAnonymousMergesKey(targetUserId: string): string {
  return `${PENDING_ANONYMOUS_MERGES_PREFIX}:${encodeURIComponent(targetUserId)}`
}

function readPendingAnonymousPrincipals(targetUserId: string): StoredAnonymousPrincipal[] {
  try {
    const raw = localStorage.getItem(pendingAnonymousMergesKey(targetUserId))
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    const valid = parsed.filter(
      (item): item is StoredAnonymousPrincipal =>
        typeof item?.access_token === 'string' &&
        typeof item?.subject === 'string' &&
        typeof item?.expires_at === 'number' &&
        item.expires_at > Date.now() + 30_000,
    )
    if (valid.length !== parsed.length) writePendingAnonymousPrincipals(targetUserId, valid)
    return valid
  } catch {
    return []
  }
}

function writePendingAnonymousPrincipals(targetUserId: string, principals: StoredAnonymousPrincipal[]): void {
  const storageKey = pendingAnonymousMergesKey(targetUserId)
  if (principals.length === 0) {
    localStorage.removeItem(storageKey)
    notifyAuthSessionChanged()
    return
  }
  localStorage.setItem(storageKey, JSON.stringify(principals))
  notifyAuthSessionChanged()
}

function tokenExpiresSoon(accessToken: string): boolean {
  try {
    const payload = accessToken.split('.')[1]
    if (!payload) return false
    const normalized = payload.replace(/-/g, '+').replace(/_/g, '/')
    const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, '=')
    const parsed = JSON.parse(atob(padded)) as { exp?: unknown }
    return typeof parsed.exp === 'number' && parsed.exp * 1000 <= Date.now() + 30_000
  } catch {
    return false
  }
}
