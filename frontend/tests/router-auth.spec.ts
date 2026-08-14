import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, expect, it, vi } from 'vitest'
import { router, safeRedirectPath } from '../src/router'
import { useAuthStore } from '../src/stores/auth'

beforeEach(async () => {
  setActivePinia(createPinia())
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { status: 401 })))
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
})

it('accepts only local non-protocol-relative login redirects', () => {
  expect(safeRedirectPath('/tasks/one?tab=report')).toBe('/tasks/one?tab=report')
  expect(safeRedirectPath('//example.test')).toBe('/')
  expect(safeRedirectPath('https://example.test')).toBe('/')
})
