import { getActivePinia } from 'pinia'
import { useAuthStore } from '../stores/auth'

function activeAuthStore() {
  return getActivePinia() ? useAuthStore() : null
}

function assertApiPath(path: string) {
  if (!path.startsWith('/api') || path.startsWith('//')) throw new Error('API requests must use a same-origin relative API path')
  const url = new URL(path, window.location.origin)
  if (url.origin !== window.location.origin || !url.pathname.startsWith('/api/')) throw new Error('API requests must use a same-origin relative API path')
}

function requestHeaders(init: RequestInit, accessToken: string | null) {
  const headers: Record<string, string> = {}
  new Headers(init.headers).forEach((value, name) => { headers[name] = value })
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`
  return headers
}

async function stableError(response: Response): Promise<string> {
  try {
    const payload = await response.json() as { error?: { message?: unknown } }
    if (typeof payload.error?.message === 'string' && payload.error.message) return payload.error.message
  } catch {
    // Never surface an arbitrary response body in the UI.
  }
  if (response.status === 504) return '后端处理超时，通常是模型接口响应太慢或网络不稳定。请稍后重试，或检查模型 Provider 配置。'
  if (response.status === 503) return '后端服务暂时不可用，请检查模型 Provider、任务队列或 Docker 服务状态。'
  if (response.status === 502) return '前端网关无法连接后端 API，请检查 api 容器是否正常运行。'
  return 'Request failed'
}

async function redirectToLogin() {
  activeAuthStore()?.clearAuth()
  window.dispatchEvent(new Event('secagent:auth-expired'))
}

function isRefreshPath(path: string) {
  return new URL(path, window.location.origin).pathname === '/api/auth/refresh'
}

async function send(path: string, init: RequestInit, includeAccessToken: boolean): Promise<Response> {
  const auth = activeAuthStore()
  return fetch(path, {
    ...init,
    credentials: 'same-origin',
    headers: requestHeaders(init, includeAccessToken ? auth?.accessToken ?? null : null),
  })
}

async function responseFor(path: string, init: RequestInit = {}, retried = false): Promise<Response> {
  assertApiPath(path)
  const refreshRequest = isRefreshPath(path)
  const response = await send(path, init, !refreshRequest)
  if (response.status !== 401 || retried || refreshRequest) return response

  const auth = activeAuthStore()
  if (!auth) return response
  try {
    await auth.refresh()
  } catch {
    await redirectToLogin()
    return response
  }
  const retry = await responseFor(path, init, true)
  if (retry.status === 401) await redirectToLogin()
  return retry
}

export async function apiRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await responseFor(path, init)
  if (!response.ok) throw new Error(await stableError(response))
  return response.json() as Promise<T>
}

export async function apiTextRequest(path: string, init: RequestInit = {}): Promise<string> {
  const response = await responseFor(path, init)
  if (!response.ok) throw new Error(await stableError(response))
  return response.text()
}
