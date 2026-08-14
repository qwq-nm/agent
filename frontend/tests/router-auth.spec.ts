import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, expect, it, vi } from 'vitest'
import { router, safeRedirectPath } from '../src/router'
import { apiRequest } from '../src/api/http'
import { useAuthStore } from '../src/stores/auth'

beforeEach(async () => {
  setActivePinia(createPinia())
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { status: 401 })))
  await router.replace('/login')
  await router.replace('/')
})

it('marks application routes as authenticated and administrative routes as admin-only', () => {
  expect(router.getRoutes().find((route) => route.path === '/tasks')?.meta.requiresAuth).toBe(true)
  expect(router.getRoutes().find((route) => route.path === '/team')?.meta).toMatchObject({
    requiresAuth: true,
    requiresAdmin: true,
  })
  expect(router.getRoutes().find((route) => route.path === '/audit')?.meta).toMatchObject({
    requiresAuth: true,
    requiresAdmin: true,
  })
})

it('sends unauthenticated visitors to login with a local redirect target', async () => {
  await router.push('/tasks?status=running')

  expect(router.currentRoute.value.path).toBe('/login')
  expect(router.currentRoute.value.query.redirect).toBe('/tasks?status=running')
})

it('prevents analysts from entering administrative routes', async () => {
  const auth = useAuthStore()
  auth.accessToken = 'memory-only-token'
  auth.user = { id: 'analyst-1', username: 'alice', role: 'analyst' }

  await router.push('/team')

  expect(router.currentRoute.value.path).toBe('/')
  await router.push('/audit')
  expect(router.currentRoute.value.path).toBe('/')
})

it('allows admins to enter administrative routes', async () => {
  const auth = useAuthStore()
  auth.accessToken = 'memory-only-token'
  auth.user = { id: 'admin-1', username: 'admin', role: 'admin' }

  await router.push('/team')
  expect(router.currentRoute.value.path).toBe('/team')
  await router.push('/audit')
  expect(router.currentRoute.value.path).toBe('/audit')
})

it('accepts only normalized local login redirects', () => {
  expect(safeRedirectPath('/tasks/one?tab=report#summary')).toBe('/tasks/one?tab=report#summary')
  for (const value of [
    '/tasks?filter=a%26b',
    '/tasks?section=%23summary',
    '/tasks?next=%3Fdetails',
    '/tasks?source=https://example.test/report',
  ]) {
    expect(safeRedirectPath(value)).toBe(value)
  }
  for (const value of [
    '//example.test',
    'https://example.test',
    '/%2F%2Fevil.test',
    '/%252F%252Fevil.test',
    '/\\evil.test',
    '/%5C%5Cevil.test',
    '/%E0%A4%A',
    '/%00tasks',
  ]) {
    expect(safeRedirectPath(value)).toBe('/')
  }
  expect(safeRedirectPath('/https:%2F%2Fevil.test')).toBe('/https:%2F%2Fevil.test')
  let nested = '//evil.test'
  for (let round = 0; round < 5; round += 1) nested = encodeURIComponent(nested)
  expect(safeRedirectPath(`/${nested}`)).toBe('/')
})

it('preserves encoded redirect query semantics after login routing', async () => {
  const auth = useAuthStore()
  auth.accessToken = 'memory-only-token'
  auth.user = { id: 'analyst-1', username: 'alice', role: 'analyst' }
  const redirect = '/tasks?filter=a%26b&section=%23summary&next=%3Fdetails&source=https://example.test/report'

  await router.push({ path: '/login', query: { redirect } })

  expect(router.currentRoute.value.path).toBe('/tasks')
  expect(router.currentRoute.value.query).toMatchObject({
    filter: 'a&b',
    section: '#summary',
    next: '?details',
    source: 'https://example.test/report',
  })
})

it('clears a failed refresh session and redirects to login', async () => {
  const auth = useAuthStore()
  auth.accessToken = 'expired-token'
  auth.user = { id: 'analyst-1', username: 'alice', role: 'analyst' }
  await router.push('/tasks')
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(new Response('', { status: 401 }))
    .mockResolvedValueOnce(new Response('', { status: 401 }))
  vi.stubGlobal('fetch', fetchMock)

  await expect(apiRequest('/api/tasks')).rejects.toThrow('Request failed')
  await vi.waitFor(() => expect(router.currentRoute.value.path).toBe('/login'))

  expect(auth.accessToken).toBeNull()
  expect(auth.user).toBeNull()
  expect(fetchMock).toHaveBeenCalledTimes(2)
  expect(fetchMock.mock.calls[1][0]).toBe('/api/auth/refresh')
  expect(localStorage.getItem('access_token')).toBeNull()
  expect(sessionStorage.getItem('access_token')).toBeNull()
})

it('redirects to login after the retry also returns 401 without making a third request', async () => {
  const auth = useAuthStore()
  auth.accessToken = 'expired-token'
  auth.user = { id: 'analyst-1', username: 'alice', role: 'analyst' }
  await router.push('/tasks')
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(new Response('', { status: 401 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({
      access_token: 'replacement-token',
      user: { id: 'analyst-1', username: 'alice', role: 'analyst' },
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    .mockResolvedValueOnce(new Response('', { status: 401 }))
  vi.stubGlobal('fetch', fetchMock)

  await expect(apiRequest('/api/tasks')).rejects.toThrow('Request failed')
  await vi.waitFor(() => expect(router.currentRoute.value.path).toBe('/login'))

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
