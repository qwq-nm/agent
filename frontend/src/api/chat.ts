import { apiRequest } from './http'

export interface ConversationSummary {
  id: string
  title: string
  status: string
  active_turn_id: string | null
  created_at: string
  updated_at: string
}

export interface ConversationMessage {
  id: string
  conversation_id: string
  sequence: number
  role: string
  kind: string
  content: string
  status: string
  turn_id: string | null
  created_at: string
}

export interface ConversationAttachment {
  id: string
  original_name: string
  relative_path: string | null
  content_type: string
  size_bytes: number
  sha256: string
}

export interface ConversationTurn {
  id: string
  conversation_id: string
  plan_version: number
  status: string
  task_id: string | null
  replan_from_turn_id: string | null
}

export interface ConversationDetail {
  conversation: ConversationSummary
  messages: { message: ConversationMessage; attachments: ConversationAttachment[] }[]
  turns: ConversationTurn[]
  active_turn: ConversationTurn | null
}

export interface MessageSendResult {
  message: ConversationMessage
  attachments: ConversationAttachment[]
  turn: ConversationTurn
  replayed: boolean
}

export interface ConversationEvent {
  id: number
  conversation_id: string
  turn_id: string | null
  subtask_id: string | null
  event_type: string
  payload: Record<string, unknown>
}

export async function listConversations(): Promise<ConversationSummary[]> {
  return apiRequest<ConversationSummary[]>('/api/conversations')
}

export async function createConversation(
  title?: string,
): Promise<ConversationSummary> {
  return apiRequest<ConversationSummary>('/api/conversations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(title ? { title } : {}),
  })
}

export async function getConversation(
  conversationId: string,
): Promise<ConversationDetail> {
  return apiRequest<ConversationDetail>(
    `/api/conversations/${encodeURIComponent(conversationId)}`,
  )
}

export async function sendMessage(
  conversationId: string,
  content: string,
  idempotencyKey: string,
): Promise<MessageSendResult> {
  return apiRequest<MessageSendResult>(
    `/api/conversations/${encodeURIComponent(conversationId)}/messages`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': idempotencyKey,
      },
      body: JSON.stringify({ content, relative_paths: [] }),
    },
  )
}

export async function stopConversation(
  conversationId: string,
): Promise<{ status: string }> {
  return apiRequest<{ status: string }>(
    `/api/conversations/${encodeURIComponent(conversationId)}/stop`,
    { method: 'POST' },
  )
}

export async function decideApproval(
  approvalId: string,
  approved: boolean,
  reason = '',
): Promise<{ id: string; status: string }> {
  return apiRequest<{ id: string; status: string }>(
    `/api/approvals/${encodeURIComponent(approvalId)}/decision`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ approved, reason }),
    },
  )
}

export type FailureDecision =
  | 'retry_same'
  | 'reassign'
  | 'skip_and_replan'
  | 'terminate_turn'

export async function decideModelFailure(
  failureId: string,
  decision: FailureDecision,
  targetProvider?: 'glm' | 'deepseek',
): Promise<{ id: string; decision: string | null }> {
  return apiRequest<{ id: string; decision: string | null }>(
    `/api/model-failures/${encodeURIComponent(failureId)}/decision`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision, target_provider: targetProvider ?? null }),
    },
  )
}

export async function createEventTicket(
  conversationId: string,
): Promise<{ ticket: string; expires_in: number }> {
  return apiRequest<{ ticket: string; expires_in: number }>(
    `/api/conversations/${encodeURIComponent(conversationId)}/event-ticket`,
    { method: 'POST' },
  )
}
