<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { api, lifecycle } from '../api/client'
import type { Task } from '../types'
import { statusLabel } from '../labels'

const route = useRoute()
const tasks = ref<Task[]>([])
const selected = ref(String(route.query.task || ''))
const report = ref('')
const error = ref('')

async function loadReport(id: string) {
  selected.value = id
  error.value = ''
  try { report.value = await lifecycle.report(id) }
  catch (value) { error.value = value instanceof Error ? value.message : '报告加载失败' }
}

onMounted(async () => {
  tasks.value = (await api.listTasks()).filter((task) => ['completed', 'failed', 'failed_retryable'].includes(task.status))
  if (selected.value) await loadReport(selected.value)
})
</script>

<template>
  <section class="page">
    <header class="page-header"><div><p class="eyebrow">REPORT ARCHIVE</p><h1>安全报告</h1><p>报告文本按纯文本呈现，事实来源以任务证据账本为准。</p></div></header>
    <div class="report-layout">
      <aside class="panel report-list"><button v-for="task in tasks" :key="task.id" :class="{ active: selected === task.id }" @click="loadReport(task.id)"><strong>{{ task.goal }}</strong><small>{{ task.id.slice(0, 8) }} / {{ statusLabel(task.status) }}</small></button></aside>
      <article class="panel report-document"><p v-if="error" class="error-message">{{ error }}</p><pre v-else-if="report">{{ report }}</pre><p v-else class="empty-state">选择一项已完成或已生成阶段性报告的任务查看报告。</p></article>
    </div>
  </section>
</template>
