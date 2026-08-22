<script setup lang="ts">
import type { TaskEvent } from '../types'

defineProps<{ events?: TaskEvent[] }>()

const EVENT_LABELS: Record<string, string> = {
  'task.created': '任务创建',
  'task.parsed': '任务理解完成',
  'task.planned': '执行计划生成',
  'task.queued': '任务进入队列',
  'task.running': '任务开始执行',
  'task.waiting_human': '等待人工确认',
  'task.paused': '任务暂停',
  'task.completed': '任务完成',
  'task.failed_retryable': '任务失败，可重试',
  'task.failed': '任务失败',
  'task.cancelled': '任务取消',
  'task.budget_exhausted': '任务资源限制耗尽',
  'job.completed': '后台任务完成',
  'job.failed': '后台任务失败',
  'plan.preview_ready': '计划预览就绪',
  'agent.plan_ready': '智能体计划就绪',
  'approval.requested': '请求人工审批',
}

function eventLabel(type: string) {
  return EVENT_LABELS[type] || type
}

function formatPayload(payload: Record<string, unknown>) {
  const parts: string[] = []
  for (const [key, value] of Object.entries(payload || {})) {
    if (value === null || value === undefined || value === '') continue
    const rendered = typeof value === 'object' ? JSON.stringify(value) : String(value)
    parts.push(`${key}: ${rendered}`)
  }
  return parts.join('；') || '无额外信息'
}
</script>

<template>
  <div class="run-log-list">
    <p v-if="!events?.length" class="empty-state compact">暂无运行事件记录。</p>
    <article v-for="event in events" :key="event.id" class="run-log-card">
      <header>
        <strong>{{ eventLabel(event.event_type) }}</strong>
        <span>#{{ event.id }}</span>
      </header>
      <small>{{ new Date(event.created_at).toLocaleString() }}</small>
      <p>{{ formatPayload(event.payload) }}</p>
      <code>{{ event.event_type }}</code>
    </article>
  </div>
</template>
