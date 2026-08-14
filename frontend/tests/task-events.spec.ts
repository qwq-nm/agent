import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { installFakeEventSource } from './fakes/event-source'

const { apiRequest } = vi.hoisted(() => ({ apiRequest: vi.fn() }))
vi.mock('../src/api/http', () => ({ apiRequest }))

import { useTaskEvents } from '../src/composables/useTaskEvents'

describe('useTaskEvents', () => {
  let source: ReturnType<typeof installFakeEventSource>

  beforeEach(() => {
    vi.useFakeTimers()
    source = installFakeEventSource()
    apiRequest.mockReset().mockResolvedValue({ ticket: 'one-time-ticket' })
  })

  afterEach(() => {
    source.restore()
    vi.useRealTimers()
  })

  it('reconnects with the last event id and sends each event to the authoritative refresher', async () => {
    const onEvent = vi.fn().mockResolvedValue(undefined)
    const stream = useTaskEvents('task-1', onEvent)
    await vi.runAllTicks()
    expect(apiRequest).toHaveBeenCalledWith('/api/tasks/task-1/event-ticket', { method: 'POST', signal: expect.any(AbortSignal) })
    expect(source.latestUrl()).toBe('/api/tasks/task-1/events?ticket=one-time-ticket')

    source.emit({ lastEventId: '41', type: 'task.running', data: '{"attempt":2}' })
    await vi.runAllTicks()
    expect(onEvent).toHaveBeenCalledTimes(1)
    source.fail()
    await vi.advanceTimersByTimeAsync(1000)
    expect(source.latestUrl()).toContain('after=41')
    expect(apiRequest).toHaveBeenCalledTimes(2)
    stream.stop()
  })

  it('does not revive a stopped stream after its event ticket resolves', async () => {
    let resolveTicket!: (value: { ticket: string }) => void
    apiRequest.mockImplementationOnce(() => new Promise((resolve) => { resolveTicket = resolve }))
    const stream = useTaskEvents('task-1', vi.fn())
    stream.stop()
    resolveTicket({ ticket: 'stale-ticket' })
    await vi.runAllTicks()
    expect(source.instances()).toHaveLength(0)
  })

  it('ignores malformed event data without invoking the refresher', async () => {
    const onEvent = vi.fn()
    const stream = useTaskEvents('task-1', onEvent)
    await vi.runAllTicks()
    source.emit({ data: '{not json' })
    await vi.runAllTicks()
    expect(onEvent).not.toHaveBeenCalled()
    stream.stop()
  })
})
