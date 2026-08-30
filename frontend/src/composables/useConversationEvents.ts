import { createEventTicket } from '../api/chat'

export interface ConversationStreamEvent {
  id: number
  type: string
  payload: Record<string, unknown>
}

export interface ConversationEventStream {
  stop: () => void
}

const EVENT_TYPES = [
  'conversation.message.created',
  'turn.decomposition.started',
  'turn.decomposition.completed',
  'turn.plan.versioned',
  'turn.replan.requested',
  'turn.synthesis.started',
  'turn.cancelled',
  'turn.completed',
  'subtask.assigned',
  'subtask.queued',
  'subtask.started',
  'subtask.waiting_approval',
  'subtask.waiting_model_decision',
  'subtask.completed',
  'subtask.incomplete',
  'subtask.skipped',
  'subtask.superseded',
  'subtask.failed',
  'subtask.cancelled',
  'subtask.tool.requested',
  'subtask.tool.rejected',
  'model.failure.waiting_decision',
  'model.failure.resolved',
  'assistant.answer.delta',
  'assistant.answer.completed',
]

const MAX_RECONNECT_DELAY = 10_000

/** One-conversation SSE stream with ticket auth and Last-Event-ID resume. */
export function useConversationEvents(
  conversationId: string,
  lastKnownEventId: number,
  onEvent: (event: ConversationStreamEvent) => void,
): ConversationEventStream {
  let source: EventSource | undefined
  let abort: AbortController | undefined
  let reconnectTimer: ReturnType<typeof setTimeout> | undefined
  let generation = 0
  let lastEventId = lastKnownEventId
  let reconnectAttempt = 0
  let stopped = false

  const cancelPending = () => {
    abort?.abort()
    abort = undefined
    if (reconnectTimer) clearTimeout(reconnectTimer)
    reconnectTimer = undefined
    source?.close()
    source = undefined
  }

  const handleEvent = (event: MessageEvent) => {
    const numericId = Number(event.lastEventId)
    if (Number.isFinite(numericId) && numericId > lastEventId) {
      lastEventId = numericId
    }
    let payload: Record<string, unknown> = {}
    try {
      const parsed: unknown = JSON.parse(event.data)
      if (parsed && typeof parsed === 'object') {
        payload = parsed as Record<string, unknown>
      }
    } catch {
      return
    }
    onEvent({ id: lastEventId, type: event.type, payload })
  }

  const scheduleReconnect = () => {
    if (stopped || reconnectTimer) return
    const delay = Math.min(1000 * 2 ** reconnectAttempt, MAX_RECONNECT_DELAY)
    reconnectAttempt += 1
    reconnectTimer = setTimeout(() => {
      reconnectTimer = undefined
      void connect()
    }, delay)
  }

  const connect = async () => {
    if (stopped) return
    const current = ++generation
    cancelPending()
    const controller = new AbortController()
    abort = controller
    try {
      const ticket = await createEventTicket(conversationId)
      if (stopped || generation !== current) return
      const query = new URLSearchParams({ ticket: ticket.ticket })
      if (lastEventId > 0) query.set('after', String(lastEventId))
      const next = new EventSource(
        `/api/conversations/${encodeURIComponent(conversationId)}/events?${query.toString()}`,
      )
      if (stopped || generation !== current) {
        next.close()
        return
      }
      source = next
      next.onopen = () => {
        reconnectAttempt = 0
      }
      next.onerror = () => {
        if (source !== next || stopped || generation !== current) return
        next.close()
        source = undefined
        scheduleReconnect()
      }
      for (const eventType of EVENT_TYPES) {
        next.addEventListener(eventType, handleEvent as EventListener)
      }
    } catch {
      if (!stopped && generation === current && !controller.signal.aborted) {
        scheduleReconnect()
      }
    } finally {
      if (abort === controller) abort = undefined
    }
  }

  const stop = () => {
    stopped = true
    generation += 1
    cancelPending()
  }

  window.addEventListener('secagent:auth-cleared', stop, { once: true })
  void connect()
  return { stop }
}
