<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import MarkdownReport from '../components/MarkdownReport.vue'
import { api, lifecycle } from '../api/client'
import type { Task } from '../types'
import { statusLabel } from '../labels'

const NAV_MIN_WIDTH = 240
const NAV_DEFAULT_WIDTH = 340
const NAV_MAX_WIDTH = 520

const route = useRoute()
const tasks = ref<Task[]>([])
const selected = ref(String(route.query.task || ''))
const report = ref('')
const error = ref('')
const navWidth = ref(NAV_DEFAULT_WIDTH)
const resizing = ref(false)
const layoutRef = ref<HTMLElement | null>(null)
let pendingFrame = 0
let dragOffset = 0

const reportLayoutStyle = computed(() => ({
  '--report-nav-width': `${navWidth.value}px`,
}))

function clampNavWidth(value: number) {
  return Math.min(NAV_MAX_WIDTH, Math.max(NAV_MIN_WIDTH, value))
}

function onPointerMove(event: PointerEvent) {
  if (!resizing.value) return
  if (pendingFrame) cancelAnimationFrame(pendingFrame)
  pendingFrame = requestAnimationFrame(() => {
    pendingFrame = 0
    const rect = layoutRef.value?.getBoundingClientRect()
    if (!rect) return
    navWidth.value = clampNavWidth(rect.right - event.clientX + dragOffset)
  })
}

function stopResize() {
  if (!resizing.value) return
  if (pendingFrame) {
    cancelAnimationFrame(pendingFrame)
    pendingFrame = 0
  }
  resizing.value = false
  window.localStorage.setItem('secagent.reportNavWidth', String(navWidth.value))
  window.removeEventListener('pointermove', onPointerMove)
  window.removeEventListener('pointerup', stopResize)
}

function startResize(event: PointerEvent) {
  event.preventDefault()
  const rect = layoutRef.value?.getBoundingClientRect()
  dragOffset = rect ? navWidth.value - (rect.right - event.clientX) : 0
  resizing.value = true
  window.addEventListener('pointermove', onPointerMove)
  window.addEventListener('pointerup', stopResize)
}

async function loadReport(id: string) {
  selected.value = id
  error.value = ''
  try {
    report.value = await lifecycle.report(id)
  } catch (value) {
    error.value = value instanceof Error ? value.message : '报告加载失败'
  }
}

onMounted(async () => {
  const savedWidth = Number(window.localStorage.getItem('secagent.reportNavWidth'))
  if (Number.isFinite(savedWidth)) navWidth.value = clampNavWidth(savedWidth)
  tasks.value = (await api.listTasks()).filter((task) =>
    ['completed', 'failed', 'failed_retryable'].includes(task.status),
  )
  if (selected.value) await loadReport(selected.value)
})

onBeforeUnmount(() => {
  if (pendingFrame) cancelAnimationFrame(pendingFrame)
  window.removeEventListener('pointermove', onPointerMove)
  window.removeEventListener('pointerup', stopResize)
})
</script>

<template>
  <section class="page report-page">
    <header class="page-header">
      <div>
        <p class="eyebrow">REPORT ARCHIVE</p>
        <h1>安全报告</h1>
      </div>
    </header>

    <div ref="layoutRef" class="report-layout" :class="{ resizing }" :style="reportLayoutStyle">
      <article class="panel report-document">
        <p v-if="error" class="error-message">{{ error }}</p>
        <MarkdownReport v-else-if="report" :content="report" />
        <p v-else class="empty-state">选择一项已完成或已生成阶段性报告的任务查看报告。</p>
      </article>

      <button
        class="report-resizer"
        type="button"
        title="拖动调整导览宽度"
        aria-label="拖动调整导览宽度"
        @pointerdown="startResize"
      ></button>

      <aside class="panel report-list">
        <header class="report-list-header">
          <strong>报告导览</strong>
          <small>{{ tasks.length }} 项</small>
        </header>
        <button
          v-for="task in tasks"
          :key="task.id"
          :class="{ active: selected === task.id }"
          @click="loadReport(task.id)"
        >
          <strong>{{ task.goal }}</strong>
          <small>{{ task.id.slice(0, 8) }} / {{ statusLabel(task.status) }}</small>
        </button>
      </aside>
    </div>
  </section>
</template>
