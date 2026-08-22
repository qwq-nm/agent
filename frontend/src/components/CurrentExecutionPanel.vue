<script setup lang="ts">
import { computed } from 'vue'
import type { TaskDetail } from '../types'
import { riskLabel, statusLabel, toolNameLabel } from '../labels'

const props = defineProps<{
  task: TaskDetail
  busy?: boolean
}>()

const emit = defineEmits<{ approve: [] }>()

const runningStep = computed(() => props.task.steps.find((step) => step.status === 'running'))
const pendingStep = computed(() => props.task.steps.find((step) => step.status === 'pending'))
const completedCount = computed(() => props.task.steps.filter((step) => step.status === 'success').length)
const isActive = computed(() => ['queued', 'planning', 'planned', 'running', 'waiting_human'].includes(props.task.status))
const currentStage = computed(
  () => props.task.current_stage || runningStep.value?.name || pendingStep.value?.name || '等待下一步',
)
const heartbeatText = computed(() => props.task.worker_heartbeat_at || props.task.heartbeat_at || '暂无更新记录')
const progressText = computed(() =>
  props.task.steps.length ? `${completedCount.value}/${props.task.steps.length} 步` : '尚未生成步骤',
)
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

    <div v-if="isActive && task.status !== 'waiting_human'" class="execution-live-strip" role="status" aria-live="polite">
      <span class="live-dot" aria-hidden="true"></span>
      <div>
        <strong>系统正在执行任务</strong>
        <p>前端会自动刷新任务状态；下方时间线和证据账本会随着工具调用逐步更新。</p>
      </div>
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
