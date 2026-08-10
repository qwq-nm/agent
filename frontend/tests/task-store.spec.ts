import { setActivePinia, createPinia } from 'pinia'
import { beforeEach, expect, it, vi } from 'vitest'
import { api } from '../src/api/client'
import { useTasksStore } from '../src/stores/tasks'

vi.mock('../src/api/client', () => ({
  api: { getTask: vi.fn(), listTasks: vi.fn() },
}))

beforeEach(() => {
  setActivePinia(createPinia())
  vi.useFakeTimers()
  vi.mocked(api.getTask).mockReset()
})

it('polls running tasks and stops after a terminal response', async () => {
  vi.mocked(api.getTask)
    .mockResolvedValueOnce({ id: 't1', status: 'running' } as never)
    .mockResolvedValueOnce({ id: 't1', status: 'completed' } as never)
  const store = useTasksStore()
  await store.startPolling('t1')
  expect(api.getTask).toHaveBeenCalledTimes(1)
  await vi.advanceTimersByTimeAsync(2000)
  expect(api.getTask).toHaveBeenCalledTimes(2)
  await vi.advanceTimersByTimeAsync(4000)
  expect(api.getTask).toHaveBeenCalledTimes(2)
})
