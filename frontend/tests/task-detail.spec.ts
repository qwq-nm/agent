import { mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import EvidencePanel from '../src/components/EvidencePanel.vue'
import StepTimeline from '../src/components/StepTimeline.vue'

it('shows model route, tool, source, and confidence without hiding demo state', () => {
  const step = {
    id: 's1',
    name: '检测攻击模式',
    status: 'success',
    model_provider: 'deepseek',
    tool_name: 'attack_pattern_detector',
    route_reason: '技术复核',
  }
  const evidence = {
    id: 'e1',
    evidence_type: 'raw_line',
    source: 'access.log:42',
    content: 'GET /admin',
    confidence: 0.86,
  }
  const timeline = mount(StepTimeline, {
    props: { steps: [step], isDemo: true },
  })
  const panel = mount(EvidencePanel, { props: { evidences: [evidence] } })
  expect(timeline.text()).toContain('演示结果')
  expect(timeline.text()).toContain('DeepSeek')
  expect(panel.text()).toContain('access.log:42')
  expect(panel.text()).toContain('0.86')
})
