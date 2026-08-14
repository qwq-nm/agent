import { mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import ModelRoutePanel from '../src/components/ModelRoutePanel.vue'
import WorkerStatus from '../src/components/WorkerStatus.vue'

it('shows queue, attempt, heartbeat, and current stage', () => {
  const wrapper = mount(WorkerStatus, {
    props: { queuePosition: 2, attempt: 3, heartbeatAt: '2026-08-15T07:00:00Z', stage: 'evidence' },
  })
  expect(wrapper.text()).toContain('队列位置')
  expect(wrapper.text()).toContain('2')
  expect(wrapper.text()).toContain('尝试次数')
  expect(wrapper.text()).toContain('evidence')
})

it('renders complete, safe model metrics', () => {
  const wrapper = mount(ModelRoutePanel, {
    props: {
      calls: [{
        id: 'm1', provider: 'deepseek', model: 'deepseek-v4-pro', stage: 'plan',
        route_reason: 'analysis', latency_ms: 120, is_demo: false, prompt_tokens: 12,
        completion_tokens: 8, retry_count: 1, request_id: 'req-1', error_code: 'rate_limited',
      }],
    },
  })
  expect(wrapper.text()).toContain('12 / 8 tokens')
  expect(wrapper.text()).toContain('req-1')
  expect(wrapper.text()).toContain('rate_limited')
})
