<script setup lang="ts">
import { ref, watch } from 'vue'
import { riskLabel, safetyModeLabel } from '../labels'
import type { PendingApproval, SafetyMode } from '../types'

const props = defineProps<{
  open: boolean
  busy?: boolean
  approval?: PendingApproval | null
  safetyMode?: SafetyMode
}>()
const emit = defineEmits<{ close: []; decide: [approved: boolean, reason: string] }>()
const reason = ref('')
watch(() => props.open, (value) => { if (value) reason.value = '' })

function policyReason() {
  const mode = props.safetyMode || 'conservative'
  const risk = props.approval?.risk_level || 'medium'
  if (risk === 'medium') {
    if (mode === 'standard') return '标准模式下，中风险验证类动作必须由人工确认后才能执行。'
    if (mode === 'expert') return '专家模式下，中风险动作仍需人工确认并记录审批理由。'
    return '保守模式下，中风险工具必须经过人工确认后才能执行。'
  }
  if (risk === 'high') {
    if (mode === 'expert') return '专家模式下，高风险授权验证动作必须经过人工强确认后才能执行。'
    return `${safetyModeLabel(mode)}不允许执行高风险动作。`
  }
  return `${safetyModeLabel(mode)}允许低风险工具自动执行；当前弹窗用于人工复核授权边界。`
}
</script>

<template>
  <div v-if="open && approval" class="dialog-backdrop" role="presentation" @click.self="emit('close')">
    <section class="approval-dialog" role="dialog" aria-modal="true" aria-labelledby="approval-title">
      <p class="eyebrow">HUMAN IN THE LOOP</p><h2 id="approval-title">确认工具调用</h2><p>请核对目标、工具与参数。批准只对当前工具生效。</p>
      <dl>
        <div><dt>当前策略</dt><dd>{{ safetyModeLabel(safetyMode) }}</dd></div>
        <div><dt>确认原因</dt><dd>{{ policyReason() }}</dd></div>
        <div><dt>工具</dt><dd><code>{{ approval.tool_name }}</code></dd></div>
        <div><dt>风险</dt><dd>{{ riskLabel(approval.risk_level) }}</dd></div>
        <div><dt>参数/目标</dt><dd><code>{{ approval.params_summary }}</code></dd></div>
      </dl>
      <label class="field"><span>审批理由</span><textarea v-model="reason" placeholder="说明批准或拒绝的依据" /></label>
      <div class="dialog-actions"><button class="ghost-button danger" type="button" :disabled="busy" @click="emit('decide', false, reason || '人工拒绝')">拒绝</button><button class="primary-button" type="button" :disabled="busy" @click="emit('decide', true, reason || '已核对授权范围')">批准并继续</button></div>
    </section>
  </div>
</template>
