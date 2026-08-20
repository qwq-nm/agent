import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import TaskCreateView from '../src/views/TaskCreateView.vue'

const push = vi.fn()
vi.mock('vue-router', () => ({ useRouter: () => ({ push }) }))

beforeEach(() => {
  push.mockReset()
  vi.restoreAllMocks()
})

it('submits goal, authorization, and automatic routing', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id: 't1', status: 'created' }),
    }),
  )
  const wrapper = mount(TaskCreateView)
  await wrapper
    .get('[data-test="goal"]')
    .setValue('分析 access.log 中的异常行为')
  await wrapper
    .get('[data-test="authorization"]')
    .setValue('仅分析上传日志')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(fetch).toHaveBeenCalledWith(
    '/api/tasks',
    expect.objectContaining({ method: 'POST' }),
  )
  expect(push).toHaveBeenCalledWith('/tasks/t1')
})
