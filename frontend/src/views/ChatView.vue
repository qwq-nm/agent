<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  createConversation,
  decideApproval,
  decideModelFailure,
  getConversation,
  listConversations,
  patchConversation,
  sendMessage,
  stopConversation,
  type ConversationDetail,
  type ConversationMessage,
  type ConversationSummary,
} from '../api/chat'
import {
  useConversationEvents,
  type ConversationStreamEvent,
} from '../composables/useConversationEvents'

interface SubtaskView {
  subtaskId: string
  key: string
  provider: string
  status: string
  reason: string
}

interface ApprovalView {
  approvalId: string
  toolName: string
  subtaskKey: string
  resolved: boolean
}

interface FailureView {
  failureId: string
  stage: string
  errorCode: string
  resolved: boolean
}

interface ActivityEntry {
  id: number
  ts: string
  text: string
  tone: 'stage' | 'subtask' | 'tool' | 'synthesis' | 'info'
}

const route = useRoute()
const router = useRouter()

const conversations = ref<ConversationSummary[]>([])
const detail = ref<ConversationDetail | null>(null)
const composerText = ref('')
const sending = ref(false)
const stopping = ref(false)
const errorText = ref('')
const stream = ref<{ stop: () => void } | null>(null)
const lastEventId = ref(0)
// 聊天框右下角的模型偏好（自动协同 / 具体模型版本）
const preferredModel = ref<
  'auto' | 'deepseek' | 'deepseek-v4-flash' | 'deepseek-v4-pro' | 'deepseek-vl' | 'glm' | 'glm-5.2' | 'glm-5.3'
>('auto')

const subtasks = ref<Map<string, SubtaskView>>(new Map())
const approvals = ref<ApprovalView[]>([])
const failures = ref<FailureView[]>([])

// 实时“运作/思考过程”：阶段 + 事件流（不展示隐藏推理链，只展示阶段/子任务/工具/汇总）
const turnStage = ref<string>('idle')
const activity = ref<ActivityEntry[]>([])
let activitySeq = 0

function pushActivity(text: string, tone: ActivityEntry['tone'] = 'info'): void {
  activity.value.push({
    id: ++activitySeq,
    ts: new Date().toLocaleTimeString('zh-CN', { hour12: false }),
    text,
    tone,
  })
  if (activity.value.length > 120) activity.value = activity.value.slice(-120)
}

const stageLabels: Record<string, string> = {
  idle: '待机',
  decomposing: '拆解任务',
  scheduling: '调度子任务',
  running: '执行子任务',
  synthesizing: '汇总结论',
  waiting_tool_approval: '等待审批',
  waiting_model_decision: '等待模型决策',
  completed: '已完成',
  cancelled: '已取消',
  failed: '失败',
}

const isWorking = computed(() =>
  ['decomposing', 'scheduling', 'running', 'synthesizing'].includes(turnStage.value),
)

const THEME_STORAGE_KEY = 'secagent-chat-theme'
const THEMES = [
  { id: 'sky', label: '天蓝', dot: '#0f9aa9' },
  { id: 'sand', label: '暖沙', dot: '#d98a2b' },
  { id: 'mint', label: '薄荷', dot: '#2ba471' },
  { id: 'lavender', label: '紫藤', dot: '#7a5cd6' },
  { id: 'rose', label: '蔷薇', dot: '#d4507c' },
] as const

function initialTheme(): string {
  try {
    const stored = localStorage.getItem(THEME_STORAGE_KEY)
    return THEMES.some((item) => item.id === stored) ? (stored as string) : 'sky'
  } catch {
    return 'sky'
  }
}

const theme = ref<string>(initialTheme())
watch(theme, (value) => {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, value)
  } catch {
    // 私密模式下无法持久化，主题仅本次会话生效
  }
})

const conversationId = computed(() => {
  const value = route.params.conversationId
  return typeof value === 'string' && value !== 'new' ? value : null
})

const messages = computed<ConversationMessage[]>(() => {
  if (!detail.value) return []
  return detail.value.messages.map((item) => item.message)
})

const subtaskList = computed(() => Array.from(subtasks.value.values()))
const pendingApprovals = computed(() =>
  approvals.value.filter((item) => !item.resolved),
)
const pendingFailures = computed(() =>
  failures.value.filter((item) => !item.resolved),
)

const messageList = ref<HTMLElement | null>(null)

async function loadConversations(): Promise<void> {
  try {
    conversations.value = await listConversations()
  } catch {
    conversations.value = []
  }
}

function resetLiveState(): void {
  subtasks.value = new Map()
  approvals.value = []
  failures.value = []
  turnStage.value = 'idle'
  activity.value = []
  activitySeq = 0
  lastEventId.value = 0
}

async function loadConversation(id: string): Promise<void> {
  stream.value?.stop()
  stream.value = null
  resetLiveState()
  try {
    detail.value = await getConversation(id)
  } catch (cause) {
    errorText.value = cause instanceof Error ? cause.message : String(cause)
    return
  }
  await nextTick()
  stream.value = useConversationEvents(id, lastEventId.value, onStreamEvent)
}

function onStreamEvent(event: ConversationStreamEvent): void {
  lastEventId.value = Math.max(lastEventId.value, event.id)
  const payload = event.payload
  if (event.type === 'subtask.assigned') {
    const subtaskId = String(payload.subtask_id ?? '')
    subtasks.value.set(subtaskId, {
      subtaskId,
      key: String(payload.key ?? ''),
      provider: String(payload.assigned_provider ?? ''),
      status: 'pending_dependency',
      reason: String(payload.route_reason ?? ''),
    })
  } else if (event.type.startsWith('subtask.') && payload.subtask_id) {
    const existing = subtasks.value.get(String(payload.subtask_id))
    if (existing) {
      const status = event.type.replace('subtask.', '')
      if (
        ['queued', 'started', 'waiting_approval', 'waiting_model_decision',
         'completed', 'incomplete', 'skipped', 'superseded', 'failed', 'cancelled'].includes(status)
      ) {
        existing.status = status === 'started' ? 'running' : status
      }
      if (event.type === 'subtask.waiting_approval' && payload.approval_id) {
        approvals.value.push({
          approvalId: String(payload.approval_id),
          toolName: String(payload.tool_name ?? ''),
          subtaskKey: existing.key,
          resolved: false,
        })
      }
      if (event.type === 'subtask.started') {
        if (turnStage.value === 'scheduling') turnStage.value = 'running'
        pushActivity(`子任务「${existing.key}」开始执行（${existing.provider}）`, 'subtask')
      } else if (event.type === 'subtask.tool.requested') {
        pushActivity(`子任务「${existing.key}」调用工具 ${String(payload.tool_name ?? '')}`, 'tool')
      } else if (event.type === 'subtask.tool.rejected') {
        pushActivity(`子任务「${existing.key}」工具被拒绝：${String(payload.reason ?? '')}`, 'tool')
      } else if (event.type === 'subtask.waiting_approval') {
        turnStage.value = 'waiting_tool_approval'
        pushActivity(`子任务「${existing.key}」等待工具审批`, 'subtask')
      } else if (event.type === 'subtask.completed') {
        pushActivity(`子任务「${existing.key}」完成`, 'subtask')
      } else if (event.type === 'subtask.failed') {
        pushActivity(`子任务「${existing.key}」失败`, 'subtask')
      } else if (event.type === 'subtask.waiting_model_decision') {
        turnStage.value = 'waiting_model_decision'
        pushActivity(`子任务「${existing.key}」等待模型决策`, 'subtask')
      }
    }
  } else if (event.type === 'model.failure.waiting_decision') {
    failures.value.push({
      failureId: String(payload.failure_id ?? ''),
      stage: String(payload.stage ?? ''),
      errorCode: String(payload.error_code ?? ''),
      resolved: false,
    })
    turnStage.value = 'waiting_model_decision'
    pushActivity(`模型失败（${String(payload.stage ?? '')}），等待选择处理方式`, 'stage')
  } else if (event.type === 'model.failure.resolved') {
    const failureId = String(payload.failure_id ?? '')
    const failure = failures.value.find((item) => item.failureId === failureId)
    if (failure) failure.resolved = true
    pushActivity('模型失败已处理，继续执行', 'info')
  } else if (event.type === 'turn.decomposition.started') {
    turnStage.value = 'decomposing'
    pushActivity('正在拆解任务（DeepSeek）…', 'stage')
  } else if (event.type === 'turn.decomposition.completed') {
    pushActivity('任务拆解完成', 'stage')
  } else if (event.type === 'turn.plan.versioned') {
    turnStage.value = 'scheduling'
    pushActivity(`已生成任务图 v${String(payload.plan_version ?? '')}`, 'stage')
  } else if (event.type === 'turn.replan.requested') {
    turnStage.value = 'running'
    pushActivity('收到追问，重新规划本轮', 'stage')
  } else if (event.type === 'turn.synthesis.started') {
    turnStage.value = 'synthesizing'
    pushActivity('正在汇总最终结论（DeepSeek）…', 'stage')
  } else if (event.type === 'turn.cancelled') {
    turnStage.value = 'cancelled'
    pushActivity('本轮已停止', 'stage')
  } else if (event.type === 'turn.completed') {
    turnStage.value = 'completed'
    pushActivity('本轮已完成', 'stage')
    void refreshDetail()
  } else if (event.type === 'assistant.answer.completed') {
    turnStage.value = 'completed'
    pushActivity('答案生成完毕，本轮结束', 'synthesis')
    void refreshDetail()
  }
  void nextTick(() => {
    messageList.value?.scrollTo({ top: messageList.value.scrollHeight })
  })
}

async function refreshDetail(): Promise<void> {
  const id = conversationId.value
  if (!id) return
  try {
    detail.value = await getConversation(id)
  } catch {
    // streaming events keep the view usable when a refresh races
  }
}

watch(
  conversationId,
  (id) => {
    errorText.value = ''
    if (id) void loadConversation(id)
    else {
      stream.value?.stop()
      stream.value = null
      resetLiveState()
      detail.value = null
    }
  },
  { immediate: true },
)
void loadConversations()

async function send(): Promise<void> {
  const content = composerText.value.trim()
  if (!content || sending.value) return
  sending.value = true
  errorText.value = ''
  try {
    let id = conversationId.value
    const model = preferredModel.value === 'auto' ? null : preferredModel.value
    if (!id) {
      const created = await createConversation(undefined, model)
      id = created.id
      await loadConversations()
    } else {
      try {
        await patchConversation(id, { preferred_model: model })
      } catch {
        // 运行中或瞬时失败时忽略，不阻塞发送
      }
    }
    const result = await sendMessage(
      id,
      content,
      crypto.randomUUID(),
    )
    composerText.value = ''
    if (route.params.conversationId !== id) {
      await router.replace(`/chat/${id}`)
    } else {
      detail.value = await getConversation(id)
    }
    void result
    if (!stream.value) {
      stream.value = useConversationEvents(id, lastEventId.value, onStreamEvent)
    }
  } catch (cause) {
    errorText.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    sending.value = false
  }
}

async function stopTurn(): Promise<void> {
  const id = conversationId.value
  if (!id || stopping.value) return
  stopping.value = true
  try {
    await stopConversation(id)
    await refreshDetail()
  } catch (cause) {
    errorText.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    stopping.value = false
  }
}

async function resolveApproval(approval: ApprovalView, approved: boolean): Promise<void> {
  try {
    await decideApproval(approval.approvalId, approved, approved ? 'owner approved' : 'owner rejected')
    approval.resolved = true
    await refreshDetail()
  } catch (cause) {
    errorText.value = cause instanceof Error ? cause.message : String(cause)
  }
}

async function resolveFailure(
  failure: FailureView,
  decision: 'retry_same' | 'skip_and_replan' | 'terminate_turn',
): Promise<void> {
  try {
    await decideModelFailure(failure.failureId, decision)
    failure.resolved = true
    await refreshDetail()
  } catch (cause) {
    errorText.value = cause instanceof Error ? cause.message : String(cause)
  }
}

onBeforeUnmount(() => stream.value?.stop())

const statusLabels: Record<string, string> = {
  pending_dependency: '等待依赖',
  queued: '排队中',
  running: '执行中',
  waiting_tool_approval: '等待审批',
  waiting_model_decision: '等待决策',
  completed: '已完成',
  incomplete: '不完整',
  skipped: '已跳过',
  superseded: '已接管',
  failed: '失败',
  cancelled: '已取消',
}
</script>

<template>
  <div class="chat-workspace" :data-theme="theme">
    <aside class="chat-sidebar">
      <div class="chat-brand">SecAgent-X</div>
      <button class="chat-new-button" type="button" @click="router.push('/chat/new')">
        新对话
      </button>
      <div class="chat-theme-picker" role="group" aria-label="配色">
        <button
          v-for="item in THEMES"
          :key="item.id"
          type="button"
          class="chat-theme-dot"
          :class="{ active: theme === item.id }"
          :title="item.label"
          :aria-label="`配色：${item.label}`"
          :style="{ background: item.dot }"
          @click="theme = item.id"
        />
      </div>
      <nav class="chat-conversation-list">
        <button
          v-for="item in conversations"
          :key="item.id"
          type="button"
          class="chat-conversation-item"
          :class="{ active: item.id === conversationId }"
          @click="router.push(`/chat/${item.id}`)"
        >
          {{ item.title }}
        </button>
      </nav>
    </aside>

    <main class="chat-main">
      <div ref="messageList" class="chat-messages">
        <p v-if="!conversationId" class="chat-empty-hint">
          自由输入你的分析目标，支持多轮追问。模型会把任务拆解为可并行的小任务并给出带证据的结论。
        </p>
        <article
          v-for="message in messages"
          :key="message.id"
          class="chat-message"
          :class="message.role"
        >
          <header>{{ message.role === 'user' ? '你' : '助手' }}</header>
          <p class="chat-message-content">{{ message.content }}</p>
          <p v-if="message.kind === 'assistant_answer'" class="chat-message-meta">
            演示结果 · 事实均引用证据，推断已单独标注
          </p>
        </article>
        <div v-for="approval in pendingApprovals" :key="approval.approvalId" class="chat-card">
          <strong>工具审批</strong>
          <p>子任务 {{ approval.subtaskKey }} 申请调用 {{ approval.toolName }}</p>
          <div class="chat-card-actions">
            <button type="button" @click="resolveApproval(approval, true)">批准</button>
            <button type="button" @click="resolveApproval(approval, false)">拒绝</button>
          </div>
        </div>
        <div v-for="failure in pendingFailures" :key="failure.failureId" class="chat-card failure">
          <strong>模型失败（{{ failure.stage }}）</strong>
          <p>错误码：{{ failure.errorCode }}。系统不会自动更换模型，请选择处理方式：</p>
          <div class="chat-card-actions">
            <button type="button" @click="resolveFailure(failure, 'retry_same')">重试</button>
            <button type="button" @click="resolveFailure(failure, 'skip_and_replan')">跳过并重规划</button>
            <button type="button" @click="resolveFailure(failure, 'terminate_turn')">终止本轮</button>
          </div>
        </div>
      </div>

      <p v-if="errorText" class="chat-error">{{ errorText }}</p>

      <footer class="chat-composer">
        <textarea
          v-model="composerText"
          :disabled="sending"
          rows="3"
          placeholder="输入你的目标或追问…（Ctrl+Enter 发送）"
          @keydown.ctrl.enter.prevent="send"
        />
        <div class="chat-composer-actions">
          <select v-model="preferredModel" class="chat-model-select" aria-label="选择模型">
            <option value="auto">自动协同</option>
            <optgroup label="DeepSeek">
              <option value="deepseek-v4-flash">V4 Flash</option>
              <option value="deepseek-v4-pro">V4 Pro</option>
              <option value="deepseek-vl">V4 多模态</option>
            </optgroup>
            <optgroup label="GLM">
              <option value="glm-5.2">GLM 5.2</option>
              <option value="glm-5.3">GLM 5.3</option>
            </optgroup>
          </select>
          <button
            type="button"
            :disabled="!conversationId || stopping"
            @click="stopTurn"
          >
            停止
          </button>
          <button type="button" :disabled="sending || !composerText.trim()" @click="send">
            {{ sending ? '发送中…' : '发送' }}
          </button>
        </div>
      </footer>
    </main>

    <aside class="chat-task-tree">
      <div class="chat-live" :class="{ working: isWorking }">
        <span class="chat-live-dot"></span>
        <strong>{{ stageLabels[turnStage] ?? turnStage }}</strong>
        <small>{{ isWorking ? 'Agent 正在思考/执行…' : '待机' }}</small>
      </div>

      <div v-if="activity.length" class="chat-activity">
        <h3>运作过程</h3>
        <ol>
          <li v-for="item in activity" :key="item.id" :class="item.tone">
            <span class="chat-activity-ts">{{ item.ts }}</span>
            <span class="chat-activity-text">{{ item.text }}</span>
          </li>
        </ol>
      </div>

      <h2>任务图</h2>
      <p v-if="subtaskList.length === 0" class="chat-empty-hint">
        发送消息后，这里会实时展示子任务 DAG、模型分工与状态。
      </p>
      <ul>
        <li v-for="subtask in subtaskList" :key="subtask.subtaskId">
          <div class="chat-subtask-head">
            <strong>{{ subtask.key }}</strong>
            <span class="chat-provider" :class="subtask.provider">{{ subtask.provider }}</span>
          </div>
          <span class="chat-status">{{ statusLabels[subtask.status] ?? subtask.status }}</span>
        </li>
      </ul>
    </aside>
  </div>
</template>

<style scoped>
/* agent 工作区亮色配色系统：通过 data-theme 切换 CSS 变量 */
.chat-workspace {
  --cw-bg: #eef2f7;
  --cw-panel: #ffffff;
  --cw-chat: #f8fafc;
  --cw-border: #d8dee9;
  --cw-text: #243244;
  --cw-muted: #5a6b80;
  --cw-accent: #0f9aa9;
  --cw-accent-soft: #e4f4f6;
  --cw-accent-border: #b6e1e7;
  --cw-btn-border: #cfd8e3;

  display: grid;
  grid-template-columns: 240px 1fr 340px;
  gap: 12px;
  height: calc(100vh - 56px);
  padding: 12px;
  box-sizing: border-box;
  color: var(--cw-text);
  background: var(--cw-bg);
  border-radius: 10px;
}
.chat-workspace[data-theme='sand'] {
  --cw-bg: #f5efe4;
  --cw-panel: #fffdf9;
  --cw-chat: #fbf7ef;
  --cw-border: #e2d7c3;
  --cw-text: #3d3327;
  --cw-muted: #7d705c;
  --cw-accent: #d98a2b;
  --cw-accent-soft: #fdf0dd;
  --cw-accent-border: #f0d9b5;
  --cw-btn-border: #ddcfb8;
}
.chat-workspace[data-theme='mint'] {
  --cw-bg: #e9f4ee;
  --cw-panel: #ffffff;
  --cw-chat: #f2faf6;
  --cw-border: #cfe4d8;
  --cw-text: #1f3a2e;
  --cw-muted: #557767;
  --cw-accent: #2ba471;
  --cw-accent-soft: #e2f5ec;
  --cw-accent-border: #bfe8d4;
  --cw-btn-border: #c3dcd0;
}
.chat-workspace[data-theme='lavender'] {
  --cw-bg: #f0edf9;
  --cw-panel: #ffffff;
  --cw-chat: #f8f6fd;
  --cw-border: #d9d2ec;
  --cw-text: #2e2843;
  --cw-muted: #6a6285;
  --cw-accent: #7a5cd6;
  --cw-accent-soft: #ece5fb;
  --cw-accent-border: #d5c8f2;
  --cw-btn-border: #d3cbe4;
}
.chat-workspace[data-theme='rose'] {
  --cw-bg: #f9edf1;
  --cw-panel: #ffffff;
  --cw-chat: #fdf5f7;
  --cw-border: #ecd2da;
  --cw-text: #402931;
  --cw-muted: #86606c;
  --cw-accent: #d4507c;
  --cw-accent-soft: #fce4ea;
  --cw-accent-border: #f2c3d0;
  --cw-btn-border: #e4c6d0;
}

.chat-workspace button,
.chat-workspace textarea {
  color: var(--cw-text);
}
.chat-workspace textarea::placeholder {
  color: var(--cw-muted);
}
.chat-workspace button {
  background: var(--cw-panel);
  border: 1px solid var(--cw-btn-border);
  border-radius: 6px;
  cursor: pointer;
}
.chat-workspace button:hover:not(:disabled) {
  border-color: var(--cw-accent);
  color: var(--cw-accent);
}
.chat-workspace button:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.chat-workspace h2 {
  color: var(--cw-text);
}

.chat-sidebar,
.chat-task-tree {
  border: 1px solid var(--cw-border);
  border-radius: 8px;
  padding: 12px;
  overflow-y: auto;
  background: var(--cw-panel);
  color: var(--cw-text);
}
.chat-brand {
  font-weight: 700;
  margin-bottom: 12px;
}
.chat-new-button,
.chat-conversation-item {
  display: block;
  width: 100%;
  text-align: left;
  padding: 8px;
  margin-bottom: 6px;
  border: 1px solid var(--cw-border);
  border-radius: 6px;
  background: var(--cw-panel);
  cursor: pointer;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: break-word;
  line-height: 1.45;
}
.chat-conversation-item.active {
  border-color: var(--cw-accent);
  background: var(--cw-accent-soft);
}
.chat-theme-picker {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
  padding: 2px;
}
.chat-theme-dot {
  width: 18px;
  height: 18px;
  padding: 0;
  border-radius: 50%;
  border: 2px solid var(--cw-panel);
  box-shadow: 0 0 0 1px var(--cw-border);
}
.chat-theme-dot.active {
  box-shadow: 0 0 0 2px var(--cw-accent);
}
.chat-main {
  display: flex;
  flex-direction: column;
  border: 1px solid var(--cw-border);
  border-radius: 8px;
  background: var(--cw-chat);
  color: var(--cw-text);
  min-height: 0;
}
.chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 12px;
}
.chat-message {
  margin-bottom: 10px;
  padding: 10px;
  border-radius: 8px;
  background: var(--cw-panel);
  border: 1px solid var(--cw-border);
}
.chat-message header {
  color: var(--cw-muted);
  font-size: 12px;
  font-weight: 600;
}
.chat-message.user {
  background: var(--cw-accent-soft);
  border-color: var(--cw-accent-border);
}
.chat-message.assistant {
  background: var(--cw-panel);
}
.chat-message-content {
  white-space: pre-wrap;
  margin: 6px 0 0;
}
.chat-message-meta {
  color: var(--cw-muted);
  font-size: 12px;
}
.chat-empty-hint {
  color: var(--cw-muted);
}
.chat-card {
  border: 1px solid var(--cw-accent-border);
  background: var(--cw-accent-soft);
  border-radius: 12px;
  padding: 12px 14px;
  margin-bottom: 10px;
  color: var(--cw-text);
}
.chat-card strong {
  color: var(--cw-accent);
}
.chat-card.failure {
  border-color: #f56c6c;
  background: #fef0f0;
  color: #7c2d35;
}
.chat-card-actions {
  display: flex;
  gap: 8px;
  margin-top: 10px;
}
.chat-card-actions button {
  border: 1px solid var(--cw-accent-border);
  background: var(--cw-panel);
  color: var(--cw-text);
  border-radius: 6px;
  padding: 5px 10px;
  cursor: pointer;
  font-size: 12px;
}
.chat-card-actions button:hover {
  border-color: var(--cw-accent);
  color: var(--cw-accent);
}
.chat-error {
  color: #d4507c;
  padding: 0 12px;
}
.chat-composer {
  border-top: 1px solid var(--cw-border);
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.chat-composer textarea {
  width: 100%;
  box-sizing: border-box;
  resize: vertical;
  min-height: 72px;
  background: var(--cw-panel);
  border: 1px solid var(--cw-btn-border);
  border-radius: 10px;
  padding: 10px 12px;
  line-height: 1.6;
  transition: border-color .15s ease, box-shadow .15s ease;
}
.chat-composer textarea:focus {
  outline: none;
  border-color: var(--cw-accent);
  box-shadow: 0 0 0 3px var(--cw-accent-soft);
}
.chat-composer-actions {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 8px;
}
.chat-model-select {
  border: 1px solid var(--cw-btn-border);
  border-radius: 6px;
  background: var(--cw-panel);
  color: var(--cw-text);
  padding: 6px 8px;
  font-size: 12px;
}
.chat-model-select:focus {
  outline: none;
  border-color: var(--cw-accent);
}
.chat-task-tree ul {
  list-style: none;
  padding: 0;
  margin: 0;
}
.chat-task-tree li {
  border: 1px solid var(--cw-border);
  border-radius: 6px;
  padding: 8px;
  margin-bottom: 8px;
}
.chat-live {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px;
  margin-bottom: 12px;
  border: 1px solid var(--cw-border);
  border-radius: 6px;
  background: var(--cw-accent-soft);
}
.chat-live strong {
  font-size: 13px;
  color: var(--cw-text);
}
.chat-live small {
  font-size: 11px;
  color: var(--cw-muted);
  margin-left: auto;
}
.chat-live-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--cw-muted);
  flex: 0 0 auto;
}
.chat-live.working {
  border-color: var(--cw-accent-border);
}
.chat-live.working .chat-live-dot {
  background: var(--cw-accent);
  animation: chat-live-pulse 1.1s ease-in-out infinite;
}
.chat-activity {
  margin-bottom: 12px;
  border: 1px solid var(--cw-border);
  border-radius: 6px;
  padding: 10px;
  background: var(--cw-chat);
  max-height: 260px;
  overflow-y: auto;
}
.chat-activity h3 {
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--cw-muted);
  text-transform: uppercase;
  letter-spacing: 0.6px;
}
.chat-activity ol {
  list-style: none;
  padding: 0;
  margin: 0;
  display: grid;
  gap: 6px;
}
.chat-activity li {
  display: grid;
  grid-template-columns: 44px 1fr;
  gap: 8px;
  align-items: baseline;
  font-size: 12px;
}
.chat-activity-ts {
  color: var(--cw-muted);
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 10px;
}
.chat-activity-text {
  color: var(--cw-text);
}
.chat-activity li.stage .chat-activity-text {
  font-weight: 600;
}
.chat-activity li.tool .chat-activity-text,
.chat-activity li.synthesis .chat-activity-text {
  color: var(--cw-accent);
}
@keyframes chat-live-pulse {
  0% { box-shadow: 0 0 0 0 rgba(15,154,169,0.4); }
  70% { box-shadow: 0 0 0 8px rgba(15,154,169,0); }
  100% { box-shadow: 0 0 0 0 rgba(15,154,169,0); }
}
.chat-subtask-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.chat-provider {
  font-size: 12px;
  padding: 2px 6px;
  border-radius: 4px;
  background: var(--cw-accent-soft);
  color: var(--cw-accent);
}
.chat-provider.deepseek {
  background: #f0f9eb;
  color: #67c23a;
}
.chat-status {
  font-size: 12px;
  color: var(--cw-muted);
}
@media (max-width: 900px) {
  .chat-workspace {
    grid-template-columns: 1fr;
  }
  .chat-sidebar,
  .chat-task-tree {
    display: none;
  }
}
</style>
