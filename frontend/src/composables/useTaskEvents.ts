import { apiRequest } from '../api/http'

export interface TaskStreamEvent {
  id: string
  type: string
  data: unknown
}

export interface TaskEventStream {
  stop: () => void
}

const EVENT_TYPES = [
  'task.created', 'task.queued', 'task.running', 'task.waiting_human',
  'task.paused', 'task.completed', 'task.failed_retryable', 'task.failed',
  'task.cancelled', 'task.budget_exhausted', 'job.completed', 'job.failed',
  'job.paused', 'job.cancelled',
]

const MAX_RECONNECT_DELAY = 10_000

/** A one-task, in-memory SSE stream. Tickets are never persisted or logged. */
export function useTaskEvents(
  taskId: string,
  onEvent: (event: TaskStreamEvent) => void | Promise<void>,
): TaskEventStream {
  let source: EventSource | undefined
  let abort: AbortController | undefined
  let reconnectTimer: ReturnType<typeof setTimeout> | undefined
  let generation = 0
  let lastEventId = ''
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

  const handleMessage = (event: MessageEvent) => {
    let data: unknown
    try {
      data = JSON.parse(event.data)
    } catch {
      return
    }
    if (event.lastEventId) lastEventId = event.lastEventId
    void onEvent({ id: event.lastEventId || lastEventId, type: event.type, data })
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
      const ticket = await apiRequest<{ ticket: string }>(`/api/tasks/${encodeURIComponent(taskId)}/event-ticket`, {
        method: 'POST',
        signal: controller.signal,
      })
      if (stopped || generation !== current || controller.signal.aborted) return
      const query = new URLSearchParams({ ticket: ticket.ticket })
      if (lastEventId) query.set('after', lastEventId)
      const next = new EventSource(`/api/tasks/${encodeURIComponent(taskId)}/events?${query.toString()}`)
      if (stopped || generation !== current) {
        next.close()
        return
      }
      source = next
      next.onopen = () => { reconnectAttempt = 0 }
      next.onerror = () => {
        if (source !== next || stopped || generation !== current) return
        next.close()
        source = undefined
        scheduleReconnect()
      }
      next.onmessage = handleMessage
      // Register every event emitted by the durable backend event contract.
      for (const eventType of EVENT_TYPES) next.addEventListener(eventType, handleMessage)
    } catch {
      if (!stopped && generation === current && !controller.signal.aborted) scheduleReconnect()
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
  return {
    stop: () => {
      window.removeEventListener('secagent:auth-cleared', stop)
      stop()
    },
  }
}
