<script setup lang="ts">
import type { TaskStep } from '../types'

defineProps<{ steps: TaskStep[]; isDemo?: boolean }>()

function providerName(value?: string) {
  if (!value) return '策略/工具'
  if (value.toLowerCase() === 'deepseek') return 'DeepSeek'
  if (value.toLowerCase() === 'glm') return 'GLM'
  return value
}
</script>

<template>
  <div class="timeline-wrap">
    <div v-if="isDemo" class="demo-banner">演示结果 · Mock 模型产生的路由与解释</div>
    <p v-if="!steps.length" class="empty-state compact">任务尚未生成执行步骤。</p>
    <article v-for="(step, index) in steps" :key="step.id" class="timeline-item">
      <div class="timeline-rail"><span>{{ index + 1 }}</span></div>
      <div class="timeline-card">
        <header>
          <div><strong>{{ step.name }}</strong><small>{{ step.purpose || '执行授权范围内的确定性检查' }}</small></div>
          <span class="status-badge" :data-status="step.status">{{ step.status }}</span>
        </header>
        <dl>
          <div><dt>模型/节点</dt><dd>{{ providerName(step.model_provider) }}</dd></div>
          <div><dt>路由理由</dt><dd>{{ step.route_reason || '按场景策略执行' }}</dd></div>
          <div><dt>白名单工具</dt><dd><code>{{ step.tool_name || '—' }}</code></dd></div>
          <div><dt>风险级别</dt><dd>{{ step.risk_level || 'low' }}</dd></div>
        </dl>
      </div>
    </article>
  </div>
</template>
