<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  createConversation,
  decideApproval,
  decideModelFailure,
  getConversation,
  listConversations,
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

const subtasks = ref<Map<string, SubtaskView>>(new Map())
const approvals = ref<ApprovalView[]>([])
const failures = ref<FailureView[]>([])

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
      existing.status = status === 'started' ? 'running' : status
      if (event.type === 'subtask.waiting_approval' && payload.approval_id) {
        approvals.value.push({
          approvalId: String(payload.approval_id),
          toolName: String(payload.tool_name ?? ''),
          subtaskKey: existing.key,
          resolved: false,
        })
      }
    }
  } else if (event.type === 'model.failure.waiting_decision') {
    failures.value.push({
      failureId: String(payload.failure_id ?? ''),
      stage: String(payload.stage ?? ''),
      errorCode: String(payload.error_code ?? ''),
      resolved: false,
    })
  } else if (event.type === 'model.failure.resolved') {
    const failureId = String(payload.failure_id ?? '')
    const failure = failures.value.find((item) => item.failureId === failureId)
    if (failure) failure.resolved = true
  } else if (event.type === 'assistant.answer.completed') {
    void refreshDetail()
  } else if (event.type === 'turn.completed') {
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
    if (!id) {
      const created = await createConversation()
      id = created.id
      await loadConversations()
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
  <div class="chat-workspace">
    <aside class="chat-sidebar">
      <div class="chat-brand">SecAgent-X</div>
      <button class="chat-new-button" type="button" @click="router.push('/chat/new')">
        新对话
      </button>
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
/* 亮色模式作用域：agent 工作区在深色控制台内使用独立浅色主题 */
.chat-workspace {
  display: grid;
  grid-template-columns: 240px 1fr 300px;
  gap: 12px;
  height: calc(100vh - 56px);
  padding: 12px;
  box-sizing: border-box;
  color: #243244;
  background: #eef2f7;
  border-radius: 10px;
}
.chat-workspace button,
.chat-workspace textarea {
  color: #243244;
}
.chat-workspace textarea::placeholder {
  color: #8ea0b5;
}
.chat-workspace button {
  background: #fff;
  border: 1px solid #cfd8e3;
  border-radius: 6px;
  cursor: pointer;
}
.chat-workspace button:hover:not(:disabled) {
  border-color: #409eff;
  color: #409eff;
}
.chat-workspace button:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.chat-workspace h2 {
  color: #243244;
}
.chat-sidebar,
.chat-task-tree {
  border: 1px solid #d8dee9;
  border-radius: 8px;
  padding: 12px;
  overflow-y: auto;
  background: #fff;
  color: #243244;
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
  border: 1px solid #d8dee9;
  border-radius: 6px;
  background: #fff;
  cursor: pointer;
}
.chat-conversation-item.active {
  border-color: #409eff;
  background: #ecf5ff;
}
.chat-main {
  display: flex;
  flex-direction: column;
  border: 1px solid #d8dee9;
  border-radius: 8px;
  background: #f8fafc;
  color: #243244;
  min-height: 0;
}
.chat-message header {
  color: #5a6b80;
  font-size: 12px;
  font-weight: 600;
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
  background: #fff;
  border: 1px solid #e4e9f0;
}
.chat-message.user {
  background: #e8f3ff;
  border-color: #c5e1ff;
}
.chat-message.assistant {
  background: #fff;
}
.chat-message-content {
  white-space: pre-wrap;
  margin: 6px 0 0;
}
.chat-message-meta {
  color: #7c8ba1;
  font-size: 12px;
}
.chat-empty-hint {
  color: #909399;
}
.chat-card {
  border: 1px solid #e6a23c;
  background: #fdf6ec;
  border-radius: 8px;
  padding: 10px;
  margin-bottom: 10px;
}
.chat-card.failure {
  border-color: #f56c6c;
  background: #fef0f0;
}
.chat-card-actions {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}
.chat-error {
  color: #f56c6c;
  padding: 0 12px;
}
.chat-composer {
  border-top: 1px solid #d8dee9;
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.chat-composer textarea {
  width: 100%;
  box-sizing: border-box;
  resize: vertical;
}
.chat-composer-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
.chat-task-tree ul {
  list-style: none;
  padding: 0;
  margin: 0;
}
.chat-task-tree li {
  border: 1px solid #e4e9f0;
  border-radius: 6px;
  padding: 8px;
  margin-bottom: 8px;
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
  background: #ecf5ff;
  color: #409eff;
}
.chat-provider.deepseek {
  background: #f0f9eb;
  color: #67c23a;
}
.chat-status {
  font-size: 12px;
  color: #5a6b80;
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
