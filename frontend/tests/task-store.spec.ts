import { setActivePinia, createPinia } from 'pinia'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '../src/api/client'
import { useTasksStore } from '../src/stores/tasks'
import { installFakeEventSource } from './fakes/event-source'

vi.mock('../src/api/client', () => ({
  api: { getTask: vi.fn(), listTasks: vi.fn() },
}))
vi.mock('../src/api/http', () => ({ apiRequest: vi.fn().mockResolvedValue({ ticket: 'ticket' }) }))

beforeEach(() => {
  setActivePinia(createPinia())
  vi.useFakeTimers()
  vi.mocked(api.getTask).mockReset()
})

afterEach(() => vi.useRealTimers())

it('refreshes active tasks from SSE and stops after a terminal response', async () => {
  const source = installFakeEventSource()
  vi.mocked(api.getTask)
    .mockResolvedValueOnce({ id: 't1', status: 'running' } as never)
    .mockResolvedValueOnce({ id: 't1', status: 'completed' } as never)
  const store = useTasksStore()
  await store.watchTask('t1')
  expect(api.getTask).toHaveBeenCalledTimes(1)
  source.emit({ lastEventId: '1', data: '{}' })
  await vi.runAllTicks()
  expect(api.getTask).toHaveBeenCalledTimes(2)
  await vi.advanceTimersByTimeAsync(10_000)
  expect(api.getTask).toHaveBeenCalledTimes(2)
  source.restore()
})

it('does not open a stream when logout wins the initial detail race', async () => {
  const source = installFakeEventSource()
  let resolveTask!: (value: never) => void
  vi.mocked(api.getTask).mockImplementationOnce(() => new Promise((resolve) => { resolveTask = resolve }) as never)
  const store = useTasksStore()
  const watching = store.watchTask('t1')
  window.dispatchEvent(new Event('secagent:auth-cleared'))
  resolveTask({ id: 't1', status: 'running' } as never)
  await watching
  expect(source.instances()).toHaveLength(0)
  expect(store.detail).toBeUndefined()
  source.restore()
})

it('keeps the newer task detail when an older initial request resolves last', async () => {
  let resolveFirst!: (value: never) => void
  vi.mocked(api.getTask)
    .mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve }) as never)
    .mockResolvedValueOnce({ id: 't2', status: 'completed' } as never)
  const store = useTasksStore()
  const first = store.watchTask('t1')
  await store.watchTask('t2')
  resolveFirst({ id: 't1', status: 'running' } as never)
  await first
  expect(store.detail?.id).toBe('t2')
})

it('counts queued tasks as active work', () => {
  const store = useTasksStore()
  store.tasks = [{ id: 'queued', status: 'queued' }, { id: 'completed', status: 'completed' }] as never
  expect(store.activeCount).toBe(1)
})
