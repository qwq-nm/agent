import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  workers: vi.fn(),
  readiness: vi.fn(),
  modelStatus: vi.fn(),
  providerCheck: vi.fn(),
  toolStatus: vi.fn(),
  listProviderCredentials: vi.fn(),
  saveProviderCredential: vi.fn(),
  clearProviderCredential: vi.fn(),
}))

vi.mock('../src/api/client', () => ({ api }))

import SystemView from '../src/views/SystemView.vue'

beforeEach(() => {
  vi.clearAllMocks()
  api.workers.mockResolvedValue({ online: 1, active: 2, capacity: 3, queued: 4 })
  api.readiness.mockResolvedValue({ status: 'ok', checks: { database: 'ok' } })
  api.modelStatus.mockResolvedValue([
    { name: 'deepseek', configured: true, model: 'deepseek-v4-pro', status: 'ready' },
  ])
  api.toolStatus.mockResolvedValue([])
  api.listProviderCredentials.mockResolvedValue([
    { provider: 'deepseek', configured: true, key_hint: '...abcd', updated_at: '2026-08-15T02:00:00Z' },
    { provider: 'glm', configured: false, key_hint: null, updated_at: null },
  ])
  api.saveProviderCredential.mockResolvedValue({ provider: 'deepseek', configured: true, key_hint: '...wxyz', updated_at: '2026-08-15T03:00:00Z' })
  api.clearProviderCredential.mockResolvedValue(undefined)
  api.providerCheck.mockResolvedValue({
    provider: 'deepseek', model: 'deepseek-v4-pro', status: 'ok', request_id: 'req-1', input_tokens: 12, output_tokens: 8, latency_ms: 120, error_code: null,
  })
})

it('renders safe credential hints and never a complete key', async () => {
  const wrapper = mount(SystemView)
  await flushPromises()

  expect(wrapper.get('input[name="provider-key-deepseek"]').attributes('type')).toBe('password')
  expect(wrapper.get('input[name="provider-key-glm"]').attributes('type')).toBe('password')
  expect(wrapper.text()).toContain('...abcd')
  expect(wrapper.text()).not.toContain('sk-test-secret-complete')
})

it('saves a typed provider key then clears the local input and refreshes statuses', async () => {
  const wrapper = mount(SystemView)
  await flushPromises()
  const input = wrapper.get('input[name="provider-key-deepseek"]')

  await input.setValue('sk-test-secret-complete')
  await wrapper.get('[data-action="save-provider-deepseek"]').trigger('click')
  await flushPromises()

  expect(api.saveProviderCredential).toHaveBeenCalledWith('deepseek', 'sk-test-secret-complete')
  expect((input.element as HTMLInputElement).value).toBe('')
  expect(api.listProviderCredentials).toHaveBeenCalledTimes(2)
  expect(wrapper.text()).not.toContain('sk-test-secret-complete')
})

it('clears the local input and displays a safe error when save fails', async () => {
  api.saveProviderCredential.mockRejectedValueOnce(new Error('Credential storage is unavailable'))
  const wrapper = mount(SystemView)
  await flushPromises()
  const input = wrapper.get('input[name="provider-key-deepseek"]')

  await input.setValue('sk-failed-save-secret')
  await wrapper.get('[data-action="save-provider-deepseek"]').trigger('click')
  await flushPromises()

  expect((input.element as HTMLInputElement).value).toBe('')
  expect(wrapper.text()).toContain('Credential storage is unavailable')
  expect(wrapper.text()).not.toContain('sk-failed-save-secret')
})

it('confirms before clearing a provider key and refreshes statuses', async () => {
  const wrapper = mount(SystemView)
  await flushPromises()
  const input = wrapper.get('input[name="provider-key-deepseek"]')

  await input.setValue('sk-unsaved-replacement')
  await wrapper.get('[data-action="clear-provider-deepseek"]').trigger('click')
  expect(wrapper.get('[data-form="clear-provider"]').exists()).toBe(true)
  expect(api.clearProviderCredential).not.toHaveBeenCalled()
  await wrapper.get('[data-form="clear-provider"]').trigger('submit')
  await flushPromises()

  expect(api.clearProviderCredential).toHaveBeenCalledWith('deepseek')
  expect(api.listProviderCredentials).toHaveBeenCalledTimes(2)
  expect((input.element as HTMLInputElement).value).toBe('')
})

it('clears the local input and displays a safe error when clear fails', async () => {
  api.clearProviderCredential.mockRejectedValueOnce(new Error('Provider credential clear was rejected'))
  const wrapper = mount(SystemView)
  await flushPromises()
  const input = wrapper.get('input[name="provider-key-deepseek"]')

  await input.setValue('sk-failed-clear-secret')
  await wrapper.get('[data-action="clear-provider-deepseek"]').trigger('click')
  await wrapper.get('[data-form="clear-provider"]').trigger('submit')
  await flushPromises()

  expect((input.element as HTMLInputElement).value).toBe('')
  expect(wrapper.text()).toContain('Provider credential clear was rejected')
  expect(wrapper.text()).not.toContain('sk-failed-clear-secret')
})

it('allows a GLM save while a DeepSeek save is pending', async () => {
  let resolveDeepseek!: () => void
  const deepseekSave = new Promise<{ provider: 'deepseek'; configured: true }>((resolve) => {
    resolveDeepseek = () => resolve({ provider: 'deepseek', configured: true })
  })
  api.saveProviderCredential.mockImplementation((provider: string) => {
    if (provider === 'deepseek') return deepseekSave
    return Promise.resolve({ provider: 'glm', configured: true })
  })
  const wrapper = mount(SystemView)
  await flushPromises()

  await wrapper.get('input[name="provider-key-deepseek"]').setValue('deepseek-pending-key')
  await wrapper.get('input[name="provider-key-glm"]').setValue('glm-concurrent-key')
  await wrapper.get('[data-action="save-provider-deepseek"]').trigger('click')

  expect(wrapper.get('[data-action="save-provider-deepseek"]').attributes('disabled')).toBeDefined()
  expect(wrapper.get('[data-action="save-provider-glm"]').attributes('disabled')).toBeUndefined()
  await wrapper.get('[data-action="save-provider-glm"]').trigger('click')
  await flushPromises()

  expect(api.saveProviderCredential).toHaveBeenCalledWith('deepseek', 'deepseek-pending-key')
  expect(api.saveProviderCredential).toHaveBeenCalledWith('glm', 'glm-concurrent-key')

  resolveDeepseek()
  await flushPromises()
})

it('opens an accessible focused clear dialog and closes it with Escape', async () => {
  const wrapper = mount(SystemView, { attachTo: document.body })
  await flushPromises()

  await wrapper.get('[data-action="clear-provider-deepseek"]').trigger('click')
  await flushPromises()
  const dialog = wrapper.get('[data-form="clear-provider"]')
  const title = wrapper.get('#clear-provider-title')

  expect(dialog.attributes('role')).toBe('dialog')
  expect(dialog.attributes('aria-modal')).toBe('true')
  expect(dialog.attributes('aria-labelledby')).toBe('clear-provider-title')
  expect(title.text()).toContain('清除 Provider 密钥')
  expect(dialog.element.contains(document.activeElement)).toBe(true)

  await dialog.trigger('keydown', { key: 'Escape' })
  expect(wrapper.find('[data-form="clear-provider"]').exists()).toBe(false)
  wrapper.unmount()
})

it('preserves provider connectivity checks and their safe metadata', async () => {
  const wrapper = mount(SystemView)
  await flushPromises()
  await wrapper.get('[data-action="provider-check-deepseek"]').trigger('click')
  await flushPromises()

  expect(api.providerCheck).toHaveBeenCalledWith('deepseek')
  expect(wrapper.text()).toContain('req-1')
  expect(wrapper.text()).toContain('120 ms')
  expect(wrapper.text()).toContain('12 / 8 tokens')
})
