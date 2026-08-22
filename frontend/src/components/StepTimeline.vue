<script setup lang="ts">
import type { PendingApproval, TaskStep, ToolCall } from '../types'
import {
  providerName,
  riskLabel,
  routeReasonLabel,
  stepImpactExplanationLabel,
  statusLabel,
  stepStatusExplanationLabel,
  stepNameLabel,
  toolCallReasonLabel,
  toolCapabilityLabel,
  toolNameLabel,
} from '../labels'

defineProps<{
  steps: TaskStep[]
  toolCalls?: ToolCall[]
  isDemo?: boolean
  pendingApproval?: PendingApproval | null
  busy?: boolean
}>()

const emit = defineEmits<{ approve: [] }>()

function callForStep(step: TaskStep, toolCalls?: ToolCall[]) {
  return [...(toolCalls || [])].reverse().find((call) => call.step_id === step.id || call.step_name === step.name)
}

function reasonForStep(step: TaskStep, call?: ToolCall) {
  if (call) return toolCallReasonLabel(call)
  return step.purpose || '根据当前场景策略和已有证据缺口，系统计划调用该工具补充事实。'
}
</script>

<template>
  <div class="timeline-wrap">
    <div v-if="isDemo" class="demo-banner">演示结果 · Mock 模型产生的路由与解释</div>
    <p v-if="!steps.length" class="empty-state compact">任务尚未生成执行步骤。</p>
    <article v-for="(step, index) in steps" :key="step.id" class="timeline-item">
      <div class="timeline-rail"><span>{{ index + 1 }}</span></div>
      <div class="timeline-card" :class="{ 'needs-approval': pendingApproval?.step_id === step.id }">
        <header>
          <div>
            <strong>{{ stepNameLabel(step) }}</strong>
            <small>执行依据：{{ reasonForStep(step, callForStep(step, toolCalls)) }}</small>
          </div>
          <span class="status-badge" :data-status="step.status">{{ statusLabel(step.status) }}</span>
        </header>
        <div class="timeline-explain">
          <p><b>工具作用</b>{{ toolCapabilityLabel(step.tool_name) }}</p>
          <p><b>执行状态说明</b>{{ stepStatusExplanationLabel(step, callForStep(step, toolCalls)) }}</p>
          <p><b>后续影响</b>{{ stepImpactExplanationLabel(step, callForStep(step, toolCalls)) }}</p>
        </div>
        <dl>
          <div>
            <dt>模型/节点</dt>
            <dd>{{ providerName(step.model_provider) }}</dd>
          </div>
          <div>
            <dt>决策依据</dt>
            <dd>{{ routeReasonLabel(step.route_reason) }}</dd>
          </div>
          <div>
            <dt>白名单工具</dt>
            <dd>
              <code>{{ toolNameLabel(step.tool_name) }}</code>
              <small v-if="step.tool_name">{{ step.tool_name }}</small>
            </dd>
          </div>
          <div>
            <dt>风险级别</dt>
            <dd>{{ riskLabel(step.risk_level) }}</dd>
          </div>
        </dl>

        <section v-if="pendingApproval?.step_id === step.id" class="timeline-approval">
          <div>
            <strong>此步骤等待人工审查</strong>
            <p>
              将调用 {{ riskLabel(pendingApproval.risk_level) }} 工具
              <code>{{ toolNameLabel(pendingApproval.tool_name) }}</code>，需要确认目标和参数仍在授权范围内。
            </p>
            <pre>{{ pendingApproval.params_summary }}</pre>
          </div>
          <button class="warning-button" :disabled="busy" @click="emit('approve')">处理审批</button>
        </section>
      </div>
    </article>
  </div>
</template>
