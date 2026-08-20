import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, expect, it, vi } from 'vitest'
import { apiRequest } from '../src/api/http'
import { useAuthStore } from '../src/stores/auth'

const analyst = { id: 'analyst-1', username: 'alice', role: 'analyst' as const }

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.restoreAllMocks()
  localStorage.clear()
  sessionStorage.clear()
})

it('keeps the refreshed access token in memory and retries a request once', async () => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(new Response('', { status: 401 }))
    .mockResolvedValueOnce(jsonResponse({ access_token: 'new-token', user: analyst }))
    .mockResolvedValueOnce(jsonResponse([{ id: 'task-1' }]))
  vi.stubGlobal('fetch', fetchMock)

  const tasks = await apiRequest<{ id: string }[]>('/api/tasks')
  const auth = useAuthStore()

  expect(tasks).toEqual([{ id: 'task-1' }])
  expect(auth.accessToken).toBe('new-token')
  expect(fetchMock).toHaveBeenNthCalledWith(
    2,
    '/api/auth/refresh',
    expect.objectContaining({ credentials: 'same-origin', method: 'POST' }),
  )
  expect(fetchMock).toHaveBeenLastCalledWith(
    '/api/tasks',
    expect.objectContaining({
      credentials: 'same-origin',
      headers: expect.objectContaining({ Authorization: 'Bearer new-token' }),
    }),
  )
  expect(localStorage.getItem('access_token')).toBeNull()
  expect(sessionStorage.getItem('access_token')).toBeNull()
})

it('shares one refresh request across simultaneous unauthorized requests', async () => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(new Response('', { status: 401 }))
    .mockResolvedValueOnce(new Response('', { status: 401 }))
    .mockResolvedValueOnce(jsonResponse({ access_token: 'new-token', user: analyst }))
    .mockResolvedValueOnce(jsonResponse({ id: 'first' }))
    .mockResolvedValueOnce(jsonResponse({ id: 'second' }))
  vi.stubGlobal('fetch', fetchMock)

  await expect(Promise.all([apiRequest('/api/first'), apiRequest('/api/second')])).resolves.toEqual([
    { id: 'first' },
    { id: 'second' },
  ])

  expect(fetchMock.mock.calls.filter(([url]) => url === '/api/auth/refresh')).toHaveLength(1)
})

it('rejects external URLs before a bearer token can be sent', async () => {
  const fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)

  await expect(apiRequest('https://example.test/api/tasks')).rejects.toThrow('same-origin')

  expect(fetchMock).not.toHaveBeenCalled()
})

it('clears an expired session after the one permitted retry without a third request', async () => {
  const auth = useAuthStore()
  auth.accessToken = 'expired-token'
  auth.user = analyst
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(new Response('', { status: 401 }))
    .mockResolvedValueOnce(jsonResponse({ access_token: 'replacement-token', user: analyst }))
    .mockResolvedValueOnce(new Response('', { status: 401 }))
  vi.stubGlobal('fetch', fetchMock)

  await expect(apiRequest('/api/tasks')).rejects.toThrow('Request failed')

  expect(fetchMock).toHaveBeenCalledTimes(3)
  expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
    '/api/tasks',
    '/api/auth/refresh',
    '/api/tasks',
  ])
  expect(auth.accessToken).toBeNull()
  expect(auth.user).toBeNull()
  expect(localStorage.getItem('access_token')).toBeNull()
  expect(sessionStorage.getItem('access_token')).toBeNull()
})

it('does not attach a bearer or recursively refresh the refresh endpoint', async () => {
  const auth = useAuthStore()
  auth.accessToken = 'existing-token'
  auth.user = analyst
  const fetchMock = vi.fn().mockResolvedValue(new Response('', { status: 401 }))
  vi.stubGlobal('fetch', fetchMock)

  await expect(apiRequest('/api/auth/refresh')).rejects.toThrow('Request failed')

  expect(fetchMock).toHaveBeenCalledTimes(1)
  expect(fetchMock).toHaveBeenCalledWith(
    '/api/auth/refresh',
    expect.objectContaining({ headers: expect.not.objectContaining({ Authorization: expect.anything() }) }),
  )
})

it('uses one pending refresh for concurrent bootstrap and refresh calls', async () => {
  let resolveResponse: (response: Response) => void = () => undefined
  const fetchMock = vi.fn().mockImplementation(() => new Promise<Response>((resolve) => { resolveResponse = resolve }))
  vi.stubGlobal('fetch', fetchMock)
  const auth = useAuthStore()

  const pending = [auth.bootstrap(), auth.bootstrap(), auth.refresh()]
  expect(fetchMock).toHaveBeenCalledTimes(1)
  resolveResponse(jsonResponse({ access_token: 'new-token', user: analyst }))
  await Promise.all(pending)
  await auth.bootstrap()

  expect(fetchMock).toHaveBeenCalledTimes(1)
  expect(auth.accessToken).toBe('new-token')
  expect(auth.user).toEqual(analyst)
  expect(localStorage.getItem('access_token')).toBeNull()
  expect(sessionStorage.getItem('access_token')).toBeNull()
})
