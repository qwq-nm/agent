import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { AuthResponse, AuthUser } from '../types'

const authPath = '/api/auth'

async function stableError(response: Response): Promise<string> {
  try {
    const payload = await response.json() as { error?: { message?: unknown } }
    if (typeof payload.error?.message === 'string' && payload.error.message) return payload.error.message
  } catch {
    // Deliberately do not expose non-JSON response bodies such as HTML error pages.
  }
  return 'Request failed'
}

async function authRequest<T>(path: string, init: RequestInit): Promise<T> {
  const response = await fetch(path, { ...init, credentials: 'same-origin' })
  if (!response.ok) throw new Error(await stableError(response))
  return response.json() as Promise<T>
}

export const useAuthStore = defineStore('auth', () => {
  const user = ref<AuthUser | null>(null)
  const accessToken = ref<string | null>(null)
  let refreshPromise: Promise<void> | null = null
  let bootstrapPromise: Promise<void> | null = null
  let bootstrapped = false

  function apply(response: AuthResponse) {
    accessToken.value = response.access_token
    user.value = response.user
  }

  function clearAuth() {
    accessToken.value = null
    user.value = null
    window.dispatchEvent(new Event('secagent:auth-cleared'))
  }

  async function login(username: string, password: string) {
    const response = await authRequest<AuthResponse>(`${authPath}/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    })
    apply(response)
    bootstrapped = true
  }

  async function refresh() {
    if (!refreshPromise) {
      refreshPromise = authRequest<AuthResponse>(`${authPath}/refresh`, { method: 'POST' })
        .then(apply)
        .catch((error: unknown) => {
          clearAuth()
          throw error
        })
        .finally(() => { refreshPromise = null })
    }
    return refreshPromise
  }

  async function logout() {
    try {
      await fetch(`${authPath}/logout`, { method: 'POST', credentials: 'same-origin' })
    } finally {
      clearAuth()
      bootstrapped = true
    }
  }

  async function bootstrap() {
    if (bootstrapped) return
    if (!bootstrapPromise) {
      bootstrapPromise = refresh().catch(() => undefined).finally(() => {
        bootstrapped = true
        bootstrapPromise = null
      })
    }
    return bootstrapPromise
  }

  return { user, accessToken, login, refresh, logout, bootstrap, clearAuth }
})
