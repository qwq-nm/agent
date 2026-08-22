<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import ApprovalDialog from '../components/ApprovalDialog.vue'
import AgentLoopPanel from '../components/AgentLoopPanel.vue'
import CurrentExecutionPanel from '../components/CurrentExecutionPanel.vue'
import EvidencePanel from '../components/EvidencePanel.vue'
import ModelRoutePanel from '../components/ModelRoutePanel.vue'
import PlanPreviewPanel from '../components/PlanPreviewPanel.vue'
import StepTimeline from '../components/StepTimeline.vue'
import { lifecycle } from '../api/client'
import { useTasksStore } from '../stores/tasks'
import {
  safetyModeLabel,
  sceneLabel,
  statusLabel,
  summarizeTaskDecision,
  toolNameLabel,
} from '../labels'

const route = useRoute()
const store = useTasksStore()
const taskId = computed(() => String(route.params.id))
const tab = ref<'evidence' | 'tools' | 'models'>('evidence')
const busy = ref(false)
const error = ref('')
const approvalOpen = ref(false)
const authExpanded = ref(false)
const pendingKeys = new Map<string, string>()
const pendingActions = new Map<string, Promise<void>>()

const task = computed(() => store.detail)
const canRun = computed(() => ['created', 'planned'].includes(task.value?.status || ''))
const canPause = computed(() => task.value?.status === 'running')
const canResume = computed(() => task.value?.status === 'paused')
const canRetry = computed(() => task.value?.status === 'failed_retryable')
const canCancel = computed(() =>
  ['created', 'planning', 'planned', 'running', 'waiting_human', 'paused', 'failed_retryable'].includes(
    task.value?.status || '',
  ),
)
const decisionSummary = computed(() => (task.value ? summarizeTaskDecision(task.value) : []))
const latestTool = computed(() => task.value?.tool_calls.at(-1))
const currentStage = computed(
  () =>
    task.value?.current_stage ||
    task.value?.steps.find((step) => step.status === 'running')?.name ||
    task.value?.steps.find((step) => step.status === 'pending')?.name ||
    '等待下一步',
)
const routeModeText = computed(() =>
  task.value?.route_mode === 'auto' ? 'GLM + DeepSeek 自动协同' : task.value?.preferred_model || '手动指定模型',
)
const authorizationSummary = computed(() => {
  const value = task.value?.authorization_scope || ''
  return value.length > 140 ? `${value.slice(0, 140)}...` : value
})

async function refresh() {
  await store.watchTask(taskId.value)
  if (store.detail?.pending_approval) approvalOpen.value = true
}

function action(name: 'run' | 'pause' | 'resume' | 'retry' | 'cancel') {
  const existing = pendingActions.get(name)
  if (existing) return existing
  const work = performAction(name)
  pendingActions.set(name, work)
  return work.finally(() => pendingActions.delete(name))
}

async function performAction(name: 'run' | 'pause' | 'resume' | 'retry' | 'cancel') {
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

function decide(approved: boolean, reason: string) {
  const existing = pendingActions.get('approve')
  if (existing) return existing
  const work = performDecision(approved, reason)
  pendingActions.set('approve', work)
  return work.finally(() => pendingActions.delete('approve'))
}

async function performDecision(approved: boolean, reason: string) {
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
  <section v-if="task" class="page">
    <header class="task-hero panel">
      <div class="task-main">
        <p class="eyebrow">TASK / {{ task.id.slice(0, 8) }}</p>
        <h1>{{ task.goal }}</h1>
        <div class="task-meta-row">
          <span class="status-badge" :data-status="task.status">{{ statusLabel(task.status) }}</span>
          <span>{{ sceneLabel(task.scene || task.scene_hint) }}</span>
          <span>{{ safetyModeLabel(task.safety_mode) }}</span>
          <span>{{ routeModeText }}</span>
          <b v-if="task.is_demo">演示结果</b>
        </div>
      </div>
      <aside class="task-now">
        <span>当前阶段</span>
        <strong>{{ currentStage }}</strong>
      </aside>
    </header>

    <section class="authorization-brief panel">
      <div>
        <strong>授权范围</strong>
        <p>{{ authExpanded ? task.authorization_scope : authorizationSummary }}</p>
      </div>
      <button class="ghost-button" type="button" @click="authExpanded = !authExpanded">
        {{ authExpanded ? '收起' : '展开完整授权' }}
      </button>
    </section>

    <div class="task-actions">
      <button v-if="canRun && !task.plan_preview" class="primary-button" :disabled="busy" @click="action('run')">
        开始执行
      </button>
      <button v-if="canPause" class="ghost-button" :disabled="busy" @click="action('pause')">暂停</button>
      <button v-if="canResume" class="primary-button" :disabled="busy" @click="action('resume')">恢复</button>
      <button v-if="canRetry" class="primary-button" :disabled="busy" @click="action('retry')">重试</button>
      <button v-if="task.pending_approval" class="warning-button" :disabled="busy" @click="approvalOpen = true">
        查看待审批动作
      </button>
      <button v-if="canCancel" class="ghost-button danger" :disabled="busy" @click="action('cancel')">取消任务</button>
      <RouterLink v-if="task.reports?.length" class="ghost-button link-button" :to="`/reports?task=${task.id}`">
        查看报告
      </RouterLink>
      <p v-if="error" class="error-message">{{ error }}</p>
    </div>

    <CurrentExecutionPanel :task="task" :busy="busy" @approve="approvalOpen = true" />

    <AgentLoopPanel :task="task" />

    <PlanPreviewPanel
      :preview="task.plan_preview"
      :can-start="task.status === 'planned'"
      :busy="busy"
      :default-collapsed="task.status !== 'planned'"
      @start="action('run')"
    />

    <section class="panel decision-panel">
      <header class="panel-title">
        <div>
          <p class="eyebrow">AGENT DECISION</p>
          <h2>自主决策说明</h2>
        </div>
        <span>中文解释</span>
      </header>
      <div class="decision-body">
        <article>
          <h3>当前判断</h3>
          <p v-for="line in decisionSummary" :key="line">{{ line }}</p>
        </article>
        <article>
          <h3>证据与工具</h3>
          <p>证据账本负责保存工具输出、模型决策依据和报告引用，后续结论都应该能回溯到这里。</p>
          <p v-if="latestTool">
            最近一次工具调用是“{{ toolNameLabel(latestTool.tool_name) }}”，内部工具名为
            <code>{{ latestTool.tool_name }}</code>。
          </p>
          <p v-else>当前还没有真实工具调用，系统仍处于任务理解或计划生成阶段。</p>
        </article>
      </div>
    </section>

    <div class="detail-grid">
      <section class="panel detail-column">
        <header class="panel-title">
          <div>
            <p class="eyebrow">DECISION TRACE</p>
            <h2>执行时间线</h2>
          </div>
          <span>{{ task.steps.length }} 步</span>
        </header>
        <StepTimeline
          :steps="task.steps"
          :is-demo="task.is_demo"
          :pending-approval="task.pending_approval"
          :busy="busy"
          @approve="approvalOpen = true"
        />
      </section>

      <aside class="panel evidence-column">
        <header class="panel-title">
          <div>
            <p class="eyebrow">EVIDENCE LEDGER</p>
            <h2>证据账本</h2>
          </div>
          <span>{{ task.evidences.length }} 条</span>
        </header>
        <div class="tabs">
          <button :class="{ active: tab === 'evidence' }" @click="tab = 'evidence'">证据</button>
          <button :class="{ active: tab === 'tools' }" @click="tab = 'tools'">工具</button>
          <button :class="{ active: tab === 'models' }" @click="tab = 'models'">模型</button>
        </div>
        <EvidencePanel v-if="tab === 'evidence'" :evidences="task.evidences" />
        <div v-else-if="tab === 'tools'" class="tool-call-list">
          <article v-for="call in task.tool_calls" :key="call.id">
            <header>
              <code>{{ toolNameLabel(call.tool_name) }}</code>
              <span>{{ statusLabel(call.status) }}</span>
            </header>
            <small>{{ call.tool_name }}</small>
            <h3>调用参数</h3>
            <pre>{{ JSON.stringify(call.params, null, 2) }}</pre>
            <h3>输出结果</h3>
            <pre>{{ JSON.stringify(call.result, null, 2) }}</pre>
          </article>
          <p v-if="!task.tool_calls.length" class="empty-state compact">暂无工具调用。</p>
        </div>
        <ModelRoutePanel v-else :calls="task.model_calls" />
      </aside>
    </div>

    <ApprovalDialog
      :open="approvalOpen"
      :busy="busy"
      :approval="task.pending_approval"
      :safety-mode="task.safety_mode"
      @close="approvalOpen = false"
      @decide="decide"
    />
  </section>
  <section v-else class="page">
    <div class="panel empty-state">正在加载任务详情...</div>
  </section>
</template>
