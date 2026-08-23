<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import ApprovalDialog from '../components/ApprovalDialog.vue'
import AgentLoopPanel from '../components/AgentLoopPanel.vue'
import CurrentExecutionPanel from '../components/CurrentExecutionPanel.vue'
import EvidencePanel from '../components/EvidencePanel.vue'
import ModelRoutePanel from '../components/ModelRoutePanel.vue'
import PlanPreviewPanel from '../components/PlanPreviewPanel.vue'
import RuntimeMemoryPanel from '../components/RuntimeMemoryPanel.vue'
import RunLogPanel from '../components/RunLogPanel.vue'
import StepTimeline from '../components/StepTimeline.vue'
import { lifecycle } from '../api/client'
import { useTasksStore } from '../stores/tasks'
import {
  safetyModeLabel,
  sceneLabel,
  statusLabel,
  stepNameLabel,
  summarizeTaskDecision,
  toolNameLabel,
} from '../labels'

const route = useRoute()
const store = useTasksStore()
const taskId = computed(() => String(route.params.id))
const tab = ref<'evidence' | 'tools' | 'models' | 'events'>('evidence')
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
const canContinue = computed(() =>
  task.value?.status === 'completed' ||
  (task.value?.status === 'failed_retryable' && Boolean(task.value?.evidences.length || task.value?.tool_calls.length)),
)
const canCancel = computed(() =>
  ['created', 'planning', 'planned', 'running', 'waiting_human', 'paused', 'failed_retryable'].includes(
    task.value?.status || '',
  ),
)
const isLive = computed(() => ['queued', 'planning', 'running', 'waiting_human'].includes(task.value?.status || ''))
const decisionSummary = computed(() => (task.value ? summarizeTaskDecision(task.value) : []))
const latestTool = computed(() => task.value?.tool_calls.at(-1))
const completedSteps = computed(() => task.value?.steps.filter((step) => step.status === 'success').length || 0)
const planRounds = computed(() => {
  const value = task.value
  if (!value?.steps.length) return []
  const events = [...(value.task_events || [])]
    .filter((event) => event.event_type === 'agent.planning_completed')
    .sort((left, right) => left.id - right.id)
  const rounds: Array<{ round: number; label: string; steps: typeof value.steps }> = []
  let offset = 0
  for (const event of events) {
    const count = Number(event.payload?.steps || 0)
    if (!Number.isFinite(count) || count <= 0) continue
    const round = Number(event.payload?.replan_round || rounds.length)
    const steps = value.steps.slice(offset, offset + count)
    if (steps.length) {
      rounds.push({
        round,
        label: round > 0 ? `第 ${round + 1} 轮继续分析规划` : '第 1 轮初始规划',
        steps,
      })
      offset += steps.length
    }
  }
  if (offset < value.steps.length) {
    rounds.push({
      round: rounds.length,
      label: rounds.length ? `第 ${rounds.length + 1} 轮规划` : '第 1 轮规划',
      steps: value.steps.slice(offset),
    })
  }
  return rounds
})
const isWaitingForNextPlan = computed(() =>
  task.value?.status === 'running' &&
  Boolean(task.value.steps.length) &&
  !task.value.steps.some((step) => ['running', 'pending'].includes(step.status)) &&
  !task.value.pending_approval,
)
const currentStage = computed(
  () =>
    task.value?.current_stage ||
    task.value?.steps.find((step) => step.status === 'running')?.name ||
    (task.value?.status === 'failed' ? '任务失败，自动分析已停止' : undefined) ||
    (task.value?.status === 'failed_retryable' ? '任务失败，可查看原因后重试' : undefined) ||
    (task.value?.status === 'completed' ? '任务已完成，报告已生成' : undefined) ||
    (task.value?.status === 'cancelled' ? '任务已取消' : undefined) ||
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

function action(name: 'run' | 'pause' | 'resume' | 'retry' | 'continue' | 'cancel') {
  const existing = pendingActions.get(name)
  if (existing) return existing
  const work = performAction(name)
  pendingActions.set(name, work)
  return work.finally(() => pendingActions.delete(name))
}

async function performAction(name: 'run' | 'pause' | 'resume' | 'retry' | 'continue' | 'cancel') {
  const key = name === 'pause' ? undefined : pendingKeys.get(name) || crypto.randomUUID()
  if (key) pendingKeys.set(name, key)
  busy.value = true
  error.value = ''
  try {
    if (name === 'pause') await lifecycle.pause(taskId.value)
    else if (name === 'run') await lifecycle.run(taskId.value, key!)
    else if (name === 'resume') await lifecycle.resume(taskId.value, key!)
    else if (name === 'retry') await lifecycle.retry(taskId.value, key!)
    else if (name === 'continue') await lifecycle.continueAnalysis(taskId.value, key!)
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
      <button v-if="canContinue" class="primary-button" :disabled="busy" @click="action('continue')">
        继续分析
      </button>
      <button v-if="task.pending_approval" class="warning-button" :disabled="busy" @click="approvalOpen = true">
        查看待审批动作
      </button>
      <button v-if="canCancel" class="ghost-button danger" :disabled="busy" @click="action('cancel')">取消任务</button>
      <RouterLink v-if="task.reports?.length" class="ghost-button link-button" :to="`/reports?task=${task.id}`">
        查看报告
      </RouterLink>
      <p v-if="error" class="error-message">{{ error }}</p>
    </div>

    <div class="task-board" :class="{ 'is-live': isLive }">
      <CurrentExecutionPanel class="board-panel" :task="task" :busy="busy" @approve="approvalOpen = true" />

      <section class="panel live-plan-panel board-panel">
        <header class="panel-title compact-title">
          <div>
            <p class="eyebrow">LIVE PLAN</p>
            <h2>执行计划</h2>
          </div>
          <span>{{ completedSteps }}/{{ task.steps.length || task.plan_preview?.steps.length || 0 }} 步</span>
        </header>
        <div class="live-plan-list">
          <section v-for="round in planRounds" :key="round.label" class="plan-round">
            <header>
              <strong>{{ round.label }}</strong>
              <small>{{ round.steps.length }} 步</small>
            </header>
            <article
              v-for="step in round.steps"
              :key="step.id"
              :class="{ active: step.status === 'running', waiting: task.pending_approval?.step_id === step.id }"
            >
              <span>{{ step.index || task.steps.indexOf(step) + 1 }}</span>
              <div>
                <strong>{{ stepNameLabel(step) }}</strong>
                <small>{{ toolNameLabel(step.tool_name) }} · {{ statusLabel(step.status) }}</small>
              </div>
            </article>
          </section>
          <article
            v-for="step in task.steps.slice(0, 0)"
            :key="step.id"
            :class="{ active: step.status === 'running', waiting: task.pending_approval?.step_id === step.id }"
          >
            <span>{{ step.index || task.steps.indexOf(step) + 1 }}</span>
            <div>
              <strong>{{ stepNameLabel(step) }}</strong>
              <small>{{ toolNameLabel(step.tool_name) }} · {{ statusLabel(step.status) }}</small>
            </div>
          </article>
          <article
            v-for="step in task.steps.length ? [] : task.plan_preview?.steps || []"
            :key="`${step.index}-${step.name}`"
          >
            <span>{{ step.index }}</span>
            <div>
              <strong>{{ step.name }}</strong>
              <small>{{ toolNameLabel(step.tool_name) }} · 待执行</small>
            </div>
          </article>
          <article v-if="isWaitingForNextPlan" class="active planning-next">
            <span>{{ task.steps.length + 1 }}</span>
            <div>
              <strong>正在生成下一轮执行计划</strong>
              <small>AI 正在根据证据账本和工具结果重新规划</small>
            </div>
          </article>
          <p v-if="!task.steps.length && !task.plan_preview" class="empty-state compact">尚未生成执行计划。</p>
        </div>
      </section>

      <PlanPreviewPanel
        class="board-panel"
        :preview="task.plan_preview"
        :can-start="task.status === 'planned'"
        :busy="busy"
        :default-collapsed="task.status !== 'planned'"
        @start="action('run')"
      />

      <section class="panel detail-column board-panel">
        <header class="panel-title">
          <div>
            <p class="eyebrow">DECISION TRACE</p>
            <h2>执行时间线</h2>
          </div>
          <span>{{ task.steps.length }} 步</span>
        </header>
        <StepTimeline
          :steps="task.steps"
          :tool-calls="task.tool_calls"
          :is-demo="task.is_demo"
          :pending-approval="task.pending_approval"
          :busy="busy"
          :auto-focus="isLive"
          @approve="approvalOpen = true"
        />
      </section>

      <section class="panel evidence-live-panel board-panel">
        <header class="panel-title">
          <div>
            <p class="eyebrow">EVIDENCE LEDGER</p>
            <h2>证据与工具</h2>
          </div>
          <span>{{ task.evidences.length }} 条</span>
        </header>
        <div class="tabs">
          <button :class="{ active: tab === 'evidence' }" @click="tab = 'evidence'">证据</button>
          <button :class="{ active: tab === 'tools' }" @click="tab = 'tools'">工具</button>
          <button :class="{ active: tab === 'models' }" @click="tab = 'models'">模型</button>
          <button :class="{ active: tab === 'events' }" @click="tab = 'events'">过程</button>
        </div>
        <EvidencePanel v-if="tab === 'evidence'" :evidences="task.evidences" :auto-focus="isLive" />
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
        <ModelRoutePanel v-else-if="tab === 'models'" :calls="task.model_calls" />
        <RunLogPanel v-else :events="task.task_events" />
      </section>

      <AgentLoopPanel class="board-panel" :task="task" :live="isLive" />
      <RuntimeMemoryPanel class="board-panel" :memory="task.runtime_memory" />

      <section class="panel decision-panel board-panel">
        <header class="panel-title">
          <div>
            <p class="eyebrow">AGENT DECISION</p>
            <h2>当前决策摘要</h2>
          </div>
          <span>{{ task.model_calls.length }} 次模型节点</span>
        </header>
        <div class="decision-body">
          <article>
            <h3>系统判断</h3>
            <p v-for="line in decisionSummary" :key="line">{{ line }}</p>
          </article>
          <article>
            <h3>最近工具结果</h3>
            <p v-if="latestTool">
              最近一次工具调用是“{{ toolNameLabel(latestTool.tool_name) }}”，内部工具名为
              <code>{{ latestTool.tool_name }}</code>。
            </p>
            <p v-else>当前还没有真实工具调用，系统仍处于任务理解或计划生成阶段。</p>
          </article>
        </div>
      </section>
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
