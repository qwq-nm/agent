<script setup lang="ts">
import { computed } from 'vue'
import type { TaskDetail } from '../types'
import { riskLabel, statusLabel, taskFailureReasonLabel, toolNameLabel } from '../labels'

const props = defineProps<{
  task: TaskDetail
  busy?: boolean
}>()

const emit = defineEmits<{ approve: [] }>()

const runningStep = computed(() => props.task.steps.find((step) => step.status === 'running'))
const pendingStep = computed(() => props.task.steps.find((step) => step.status === 'pending'))
const completedCount = computed(() => props.task.steps.filter((step) => step.status === 'success').length)
const isActive = computed(() => ['queued', 'planning', 'running', 'waiting_human'].includes(props.task.status))
const isLive = computed(() => ['queued', 'planning', 'running'].includes(props.task.status))
const currentStage = computed(() => {
  if (props.task.current_stage) return props.task.current_stage
  if (runningStep.value) return runningStep.value.name
  if (props.task.status === 'failed') return '任务失败，自动分析已停止'
  if (props.task.status === 'failed_retryable') return '任务失败，可查看原因后重试'
  if (props.task.status === 'completed') return '任务已完成，报告已生成'
  if (props.task.status === 'cancelled') return '任务已取消'
  if (pendingStep.value) return pendingStep.value.name
  return '等待下一步'
})
const heartbeatText = computed(() => props.task.worker_heartbeat_at || props.task.heartbeat_at || '暂无更新记录')
const progressText = computed(() =>
  props.task.steps.length ? `${completedCount.value}/${props.task.steps.length} 步` : '尚未生成步骤',
)
const failureReason = computed(() => taskFailureReasonLabel(props.task))
</script>

<template>
  <section class="panel current-execution" :class="{ 'is-active': isActive, 'needs-human': task.status === 'waiting_human' }">
    <header class="panel-title compact-title">
      <div>
        <p class="eyebrow">CURRENT EXECUTION</p>
        <h2>当前执行状态</h2>
      </div>
      <span class="status-badge" :data-status="task.status">{{ statusLabel(task.status) }}</span>
    </header>

    <div class="execution-grid">
      <article>
        <span>当前阶段</span>
        <strong>{{ currentStage }}</strong>
      </article>
      <article>
        <span>执行进度</span>
        <strong>{{ progressText }}</strong>
      </article>
      <article>
        <span>尝试次数</span>
        <strong>{{ task.job_attempt || 0 }}</strong>
      </article>
      <article>
        <span>后台执行更新时间</span>
        <strong>{{ heartbeatText }}</strong>
      </article>
    </div>

    <div v-if="task.status === 'planned'" class="execution-live-strip execution-ready-strip" role="status">
      <div>
        <strong>执行计划已生成</strong>
        <p>系统已完成任务理解和计划生成，点击“开始执行”后才会调用工具并更新下方时间线与证据账本。</p>
      </div>
    </div>

    <div v-else-if="isLive" class="execution-live-strip" role="status" aria-live="polite">
      <span class="live-dot" aria-hidden="true"></span>
      <div>
        <strong>系统正在执行任务</strong>
        <p>前端会自动刷新任务状态；下方时间线和证据账本会随着工具调用逐步更新。</p>
      </div>
    </div>

    <div v-if="failureReason" class="execution-failure">
      <strong>{{ failureReason.title }}</strong>
      <p>{{ failureReason.reason }}</p>
      <p>{{ failureReason.suggestion }}</p>
    </div>

    <div v-if="task.pending_approval" class="execution-approval">
      <div>
        <strong>等待人工审批</strong>
        <p>
          工具：<code>{{ toolNameLabel(task.pending_approval.tool_name) }}</code>
          · 风险：{{ riskLabel(task.pending_approval.risk_level) }}
        </p>
      </div>
      <button class="warning-button" :disabled="busy" @click="emit('approve')">处理审批</button>
    </div>
  </section>
</template>
