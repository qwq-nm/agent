<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import ApprovalDialog from '../components/ApprovalDialog.vue'
import EvidencePanel from '../components/EvidencePanel.vue'
import ModelRoutePanel from '../components/ModelRoutePanel.vue'
import StepTimeline from '../components/StepTimeline.vue'
import WorkerStatus from '../components/WorkerStatus.vue'
import { lifecycle } from '../api/client'
import { useTasksStore } from '../stores/tasks'

const route = useRoute()
const store = useTasksStore()
const taskId = computed(() => String(route.params.id))
const tab = ref<'evidence' | 'tools' | 'models'>('evidence')
const busy = ref(false)
const error = ref('')
const approvalOpen = ref(false)
const pendingKeys = new Map<string, string>()
const task = computed(() => store.detail)
const canRun = computed(() => task.value?.status === 'created')
const canPause = computed(() => task.value?.status === 'running')
const canResume = computed(() => task.value?.status === 'paused')
const canRetry = computed(() => task.value?.status === 'failed_retryable')
const canCancel = computed(() => ['created', 'running', 'waiting_human', 'paused', 'failed_retryable'].includes(task.value?.status || ''))

async function refresh() {
  await store.watchTask(taskId.value)
  if (store.detail?.pending_approval) approvalOpen.value = true
}

async function action(name: 'run' | 'pause' | 'resume' | 'retry' | 'cancel') {
  const key = name === 'pause' ? undefined : pendingKeys.get(name) || crypto.randomUUID()
  if (key) pendingKeys.set(name, key)
  busy.value = true
  error.value = ''
  try {
    if (name === 'pause') await lifecycle.pause(taskId.value)
    else if (name === 'run') await lifecycle.run(taskId.value, key!)
    else if (name === 'resume') await lifecycle.resume(taskId.value, key!)
    else if (name === 'retry') await lifecycle.retry(taskId.value, key!)
    else await lifecycle.cancel(taskId.value, key!)
    await refresh()
  } catch (value) {
    error.value = value instanceof Error ? value.message : '操作失败'
  } finally {
    if (key) pendingKeys.delete(name)
    busy.value = false
  }
}

async function decide(approved: boolean, reason: string) {
  const key = pendingKeys.get('approve') || crypto.randomUUID()
  pendingKeys.set('approve', key)
  busy.value = true
  try {
    await lifecycle.approve(taskId.value, approved, reason, key)
    approvalOpen.value = false
    await refresh()
  } catch (value) {
    error.value = value instanceof Error ? value.message : '审批失败'
  } finally {
    pendingKeys.delete('approve')
    busy.value = false
  }
}

onMounted(refresh)
watch(() => route.params.id, () => void refresh())
onBeforeUnmount(store.stopWatching)
</script>

<template>
  <section class="page" v-if="task">
    <header class="task-hero panel">
      <div><p class="eyebrow">TASK / {{ task.id.slice(0, 8) }}</p><h1>{{ task.goal }}</h1><p>{{ task.authorization_scope }}</p></div>
      <div class="task-summary"><span class="status-badge" :data-status="task.status">{{ task.status }}</span><span>{{ task.scene || task.scene_hint || '场景待识别' }}</span><span>{{ task.route_mode === 'auto' ? 'GLM + DeepSeek 自动协同' : task.preferred_model }}</span><b v-if="task.is_demo">演示结果</b></div>
    </header>
    <div class="task-actions">
      <button v-if="canRun" class="primary-button" :disabled="busy" @click="action('run')">开始执行</button>
      <button v-if="canPause" class="ghost-button" :disabled="busy" @click="action('pause')">暂停</button>
      <button v-if="canResume" class="primary-button" :disabled="busy" @click="action('resume')">恢复</button>
      <button v-if="canRetry" class="primary-button" :disabled="busy" @click="action('retry')">重试</button>
      <button v-if="task.pending_approval" class="warning-button" :disabled="busy" @click="approvalOpen = true">查看待审批动作</button>
      <button v-if="canCancel" class="ghost-button danger" :disabled="busy" @click="action('cancel')">取消任务</button>
      <RouterLink v-if="task.reports?.length" class="ghost-button link-button" :to="`/reports?task=${task.id}`">查看报告</RouterLink>
      <p v-if="error" class="error-message">{{ error }}</p>
    </div>
    <WorkerStatus :queue-position="task.queue_position" :attempt="task.job_attempt" :heartbeat-at="task.worker_heartbeat_at || task.heartbeat_at" :stage="task.current_stage || task.steps.find((step) => step.status === 'running')?.name" />
    <div class="detail-grid">
      <section class="panel detail-column"><header class="panel-title"><div><p class="eyebrow">DECISION TRACE</p><h2>执行时间线</h2></div><span>{{ task.steps.length }} 步</span></header><StepTimeline :steps="task.steps" :is-demo="task.is_demo" /></section>
      <aside class="panel evidence-column">
        <header class="panel-title"><div><p class="eyebrow">EVIDENCE LEDGER</p><h2>证据账本</h2></div><span>{{ task.evidences.length }} 条</span></header>
        <div class="tabs"><button :class="{ active: tab === 'evidence' }" @click="tab = 'evidence'">证据</button><button :class="{ active: tab === 'tools' }" @click="tab = 'tools'">工具</button><button :class="{ active: tab === 'models' }" @click="tab = 'models'">模型</button></div>
        <EvidencePanel v-if="tab === 'evidence'" :evidences="task.evidences" />
        <div v-else-if="tab === 'tools'" class="tool-call-list"><article v-for="call in task.tool_calls" :key="call.id"><header><code>{{ call.tool_name }}</code><span>{{ call.status }}</span></header><pre>{{ JSON.stringify(call.result, null, 2) }}</pre></article><p v-if="!task.tool_calls.length" class="empty-state compact">暂无工具调用。</p></div>
        <ModelRoutePanel v-else :calls="task.model_calls" />
      </aside>
    </div>
    <ApprovalDialog :open="approvalOpen" :approval="task.pending_approval" @close="approvalOpen = false" @decide="decide" />
  </section>
  <section v-else class="page"><div class="panel empty-state">正在加载任务详情…</div></section>
</template>
