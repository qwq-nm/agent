import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  workers: vi.fn(),
  readiness: vi.fn(),
  modelStatus: vi.fn(),
  providerCheck: vi.fn(),
  toolStatus: vi.fn(),
  listUsers: vi.fn(),
  createUser: vi.fn(),
  updateUser: vi.fn(),
  auditEvents: vi.fn(),
}))

vi.mock('../src/api/client', () => ({ api }))

import AuditView from '../src/views/AuditView.vue'
import SystemView from '../src/views/SystemView.vue'
import TeamView from '../src/views/TeamView.vue'

beforeEach(() => {
  vi.clearAllMocks()
  api.workers.mockResolvedValue({ online: 1, active: 2, capacity: 3, queued: 4 })
  api.readiness.mockResolvedValue({
    status: 'ok',
    checks: { database: 'ok', redis: 'ok', model_configuration: 'ok', jwt: 'ok' },
  })
  api.modelStatus.mockResolvedValue([
    { name: 'deepseek', configured: true, model: 'deepseek-v4-pro', status: 'ready' },
  ])
  api.providerCheck.mockResolvedValue({
    provider: 'deepseek',
    model: 'deepseek-v4-pro',
    status: 'ok',
    request_id: 'req-1',
    input_tokens: 12,
    output_tokens: 8,
    latency_ms: 120,
    error_code: null,
  })
  api.toolStatus.mockResolvedValue([])
  api.listUsers.mockResolvedValue([
    { id: 'u1', username: 'alice', role: 'admin', is_active: true },
    { id: 'u2', username: 'bob', role: 'analyst', is_active: false },
  ])
  api.createUser.mockResolvedValue({ id: 'u3', username: 'carol', role: 'analyst', is_active: true })
  api.updateUser.mockResolvedValue({ id: 'u2', username: 'bob', role: 'analyst', is_active: true })
  api.auditEvents.mockResolvedValue([
    {
      id: 7,
      actor_id: 'u1',
      actor_username: 'alice',
      action: 'task.run',
      resource_type: 'task',
      resource_id: 'task-1',
      outcome: 'success',
      details: { provider: 'deepseek', api_key: 'sk-should-never-render' },
      created_at: '2026-08-15T02:00:00Z',
    },
  ])
})

it('shows worker capacity and provider metadata without rendering secrets', async () => {
  const wrapper = mount(SystemView)
  await flushPromises()

  expect(wrapper.text()).toContain('2 / 3')
  expect(wrapper.text()).toContain('deepseek-v4-pro')
  expect(wrapper.text()).toContain('4')
  expect(wrapper.text()).not.toMatch(/sk-[A-Za-z0-9-]+/)
})

it('creates and re-enables a team user from the admin view', async () => {
  const wrapper = mount(TeamView)
  await flushPromises()

  expect(wrapper.text()).toContain('alice')
  expect(wrapper.text()).toContain('bob')

  await wrapper.get('[data-action="new-user"]').trigger('click')
  await wrapper.get('input[name="username"]').setValue('carol')
  await wrapper.get('input[name="password"]').setValue('password-123')
  await wrapper.get('select[name="role"]').setValue('analyst')
  await wrapper.get('form[data-form="user"]').trigger('submit')
  await flushPromises()

  expect(api.createUser).toHaveBeenCalledWith({
    username: 'carol',
    password: 'password-123',
    role: 'analyst',
  })

  await wrapper.get('[data-action="enable-u2"]').trigger('click')
  await flushPromises()
  expect(api.updateUser).toHaveBeenCalledWith('u2', { is_active: true })
})

it('renders audit rows without exposing raw secret fields', async () => {
  const wrapper = mount(AuditView)
  await flushPromises()

  expect(wrapper.text()).toContain('task.run')
  expect(wrapper.text()).toContain('alice')
  expect(wrapper.text()).toContain('task-1')
  expect(wrapper.text()).not.toContain('sk-should-never-render')
})

it('shows provider check metadata after an explicit health check', async () => {
  const wrapper = mount(SystemView)
  await flushPromises()
  await wrapper.get('[data-action="provider-check-deepseek"]').trigger('click')
  await flushPromises()

  expect(api.providerCheck).toHaveBeenCalledWith('deepseek')
  expect(wrapper.text()).toContain('req-1')
  expect(wrapper.text()).toContain('120 ms')
  expect(wrapper.text()).toContain('12 / 8 tokens')
})

it('requires the member name before disabling an account', async () => {
  api.listUsers.mockResolvedValue([
    { id: 'u1', username: 'alice', role: 'admin', is_active: true },
    { id: 'u2', username: 'bob', role: 'analyst', is_active: true },
  ])
  const wrapper = mount(TeamView)
  await flushPromises()

  await wrapper.get('[data-action="disable-u2"]').trigger('click')
  expect(wrapper.get('[data-form="disable-user"]').exists()).toBe(true)
  expect(api.updateUser).not.toHaveBeenCalled()

  const form = wrapper.get('[data-form="disable-user"]')
  const submit = form.get('[data-action="confirm-disable"]')
  expect(submit.attributes('disabled')).toBeDefined()
  await form.get('input[name="confirmation"]').setValue('bob')
  expect(submit.attributes('disabled')).toBeUndefined()
  await form.trigger('submit')
  await flushPromises()

  expect(api.updateUser).toHaveBeenCalledWith('u2', { is_active: false })
})

it('passes actor, action, outcome, and time filters to the audit API', async () => {
  const wrapper = mount(AuditView)
  await flushPromises()

  await wrapper.get('input[name="actor"]').setValue('alice')
  await wrapper.get('input[name="action"]').setValue('task.run')
  await wrapper.get('select[name="outcome"]').setValue('success')
  await wrapper.get('input[name="created-after"]').setValue('2026-08-14T00:00')
  await wrapper.get('input[name="created-before"]').setValue('2026-08-15T23:59')
  await wrapper.get('[data-form="audit-filters"]').trigger('submit')
  await flushPromises()

  expect(api.auditEvents).toHaveBeenLastCalledWith({
    limit: 100,
    before: undefined,
    actor: 'alice',
    action: 'task.run',
    outcome: 'success',
    created_after: '2026-08-14T00:00',
    created_before: '2026-08-15T23:59',
  })
})
