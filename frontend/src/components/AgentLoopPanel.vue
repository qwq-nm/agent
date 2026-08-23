<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import type { TaskDetail } from '../types'
import {
  providerName,
  sceneLabel,
  safetyModeLabel,
  stageLabel,
  statusLabel,
  stepNameLabel,
  toolCallReasonLabel,
  toolCapabilityLabel,
  toolExecutionResultLabel,
  toolNameLabel,
  toolResultSummaryLabel,
} from '../labels'

const props = defineProps<{
  task: TaskDetail
  live?: boolean
}>()

const modelLoop = ref<HTMLElement | null>(null)
const toolLoop = ref<HTMLElement | null>(null)

const recentModelCalls = computed(() => props.task.model_calls.slice(-8))
const recentToolCalls = computed(() => props.task.tool_calls.slice(-8))
const successfulSteps = computed(() => props.task.steps.filter((step) => step.status === 'success'))
const failedSteps = computed(() => props.task.steps.filter((step) => step.status.startsWith('failed')))
const latestToolCall = computed(() => props.task.tool_calls.at(-1))
const latestEvidence = computed(() => props.task.evidences.at(-1))

function modelNodeDetail(call: TaskDetail['model_calls'][number]) {
  const parts = [
    `模型：${providerName(call.provider)}`,
    `状态：${statusLabel(call.status)}`,
  ]
  if (call.latency_ms) parts.push(`耗时：${call.latency_ms} ms`)
  if (call.input_tokens || call.output_tokens) {
    parts.push(`Token：${call.input_tokens || 0} / ${call.output_tokens || 0}`)
  }
  return parts.join(' · ')
}

const taskDecisionNotes = computed(() => {
  const target = props.task.target_url || '未提供明确 URL'
  const notes = [
    `任务被归类为“${sceneLabel(props.task.scene || props.task.scene_hint)}”，目标为 ${target}，安全策略为“${safetyModeLabel(props.task.safety_mode)}”。`,
  ]
  if (props.task.steps.length) {
    notes.push(
      `模型已形成 ${props.task.steps.length} 个执行步骤：${props.task.steps
        .map((step) => `“${stepNameLabel(step)}”`)
        .join('、')}。`,
    )
  }
  if (successfulSteps.value.length) {
    notes.push(`已完成 ${successfulSteps.value.length} 个步骤，完成项包括：${successfulSteps.value.map((step) => stepNameLabel(step)).join('、')}。`)
  }
  if (failedSteps.value.length) {
    notes.push(`存在 ${failedSteps.value.length} 个失败步骤，后续分析需要优先处理前置失败或证据缺口。`)
  }
  if (latestEvidence.value) {
    notes.push(`最新证据来自 ${latestEvidence.value.source}，内容将作为模型复核和报告引用的事实依据。`)
  }
  return notes
})

const evidenceFeedback = computed(() => {
  const latest = latestToolCall.value
  if (!latest) return '当前尚无工具执行结果，模型仍处于任务理解或计划生成阶段。'
  if (latest.status === 'success') {
    return `最近一次工具“${toolNameLabel(latest.tool_name)}”已完成，关键结果为：${toolResultSummaryLabel(latest.result)}`
  }
  return `最近一次工具“${toolNameLabel(latest.tool_name)}”未成功，反馈为：${toolResultSummaryLabel(latest.result)}`
})

async function focusLatestLoopItems() {
  if (!props.live) return
  await nextTick()
  modelLoop.value?.lastElementChild?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  toolLoop.value?.lastElementChild?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
}

onMounted(focusLatestLoopItems)
watch(
  () => [props.task.model_calls.length, props.task.tool_calls.length, props.task.steps.map((step) => step.status).join('|')],
  () => void focusLatestLoopItems(),
)
</script>

<template>
  <section class="panel agent-loop-panel" :class="{ 'is-live': live }">
    <header class="panel-title compact-title">
      <div>
        <p class="eyebrow">AGENT LOOP</p>
        <h2>AI 决策与工具协同</h2>
      </div>
      <span>{{ task.model_calls.length }} 次模型节点 / {{ task.tool_calls.length }} 次工具调用</span>
    </header>

    <div class="agent-loop-grid">
      <article>
        <h3>模型决策过程</h3>
        <p v-for="note in taskDecisionNotes" :key="note">{{ note }}</p>
        <p>{{ evidenceFeedback }}</p>
        <ol v-if="recentModelCalls.length" ref="modelLoop" class="loop-list">
          <li v-for="call in recentModelCalls" :key="call.id">
            <strong>{{ stageLabel(call.stage) }}</strong>
            <span>{{ modelNodeDetail(call) }}</span>
            <small>决策依据：{{ call.route_reason }}</small>
          </li>
        </ol>
        <p v-else class="muted-text">暂无模型调用记录。</p>
      </article>

      <article>
        <h3>工具协同说明</h3>
        <p>
          以下内容来自本任务的实际工具调用记录。工具输出写入证据账本后，会作为模型复核、重规划和报告生成的上下文。
        </p>
        <ol v-if="recentToolCalls.length" ref="toolLoop" class="loop-list tool-loop-list">
          <li v-for="call in recentToolCalls" :key="call.id">
            <strong>{{ stepNameLabel(call) }}</strong>
            <span>{{ toolNameLabel(call.tool_name) }} · {{ statusLabel(call.status) }}</span>
            <small>本次调用依据：{{ toolCallReasonLabel(call) }}</small>
            <small>工具固定能力：{{ toolCapabilityLabel(call.tool_name) }}</small>
            <small>真实执行结果：{{ toolExecutionResultLabel(call.tool_name, call.result) }}</small>
          </li>
        </ol>
        <p v-else class="muted-text">暂无工具调用记录。</p>
      </article>
    </div>
  </section>
</template>
