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
