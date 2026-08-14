import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api } from '../api/client'
import type { Task, TaskDetail } from '../types'
import { useTaskEvents, type TaskEventStream } from '../composables/useTaskEvents'

const terminalStatuses = new Set(['completed', 'failed', 'cancelled'])

export const useTasksStore = defineStore('tasks', () => {
  const tasks = ref<Task[]>([])
  const loading = ref(false)
  const error = ref('')
  const detail = ref<TaskDetail>()
  let stream: TaskEventStream | undefined
  let watchedTaskId: string | undefined
  let watchGeneration = 0
  let detailRequest = 0
  let refreshInFlight: Promise<TaskDetail | undefined> | undefined
  let refreshQueued = false

  const activeCount = computed(
    () =>
      tasks.value.filter((task) =>
        ['created', 'parsed', 'planned', 'running', 'waiting_human', 'paused'].includes(
          task.status,
        ),
      ).length,
  )

  async function load() {
    loading.value = true
    error.value = ''
    try {
      tasks.value = await api.listTasks()
    } catch (value) {
      error.value = value instanceof Error ? value.message : '任务列表加载失败'
    } finally {
      loading.value = false
    }
  }

  function stopWatching() {
    watchGeneration += 1
    watchedTaskId = undefined
    stream?.stop()
    stream = undefined
    refreshQueued = false
  }

  async function refreshDetail(id: string) {
    const request = ++detailRequest
    const result = await api.getTask(id)
    if (request === detailRequest && (!watchedTaskId || watchedTaskId === id)) detail.value = result
    return result
  }

  async function refreshFromEvent(id: string, generation: number) {
    if (generation !== watchGeneration || watchedTaskId !== id) return
    if (refreshInFlight) {
      refreshQueued = true
      return refreshInFlight
    }
    refreshInFlight = refreshDetail(id).finally(() => { refreshInFlight = undefined })
    const result = await refreshInFlight
    if (generation !== watchGeneration || watchedTaskId !== id) return result
    if (terminalStatuses.has(result?.status || '')) {
      stopWatching()
      return result
    }
    if (refreshQueued) {
      refreshQueued = false
      return refreshFromEvent(id, generation)
    }
    return result
  }

  async function watchTask(id: string) {
    stopWatching()
    watchedTaskId = id
    const generation = watchGeneration
    const result = await refreshDetail(id)
    if (generation !== watchGeneration || watchedTaskId !== id || terminalStatuses.has(result.status)) return result
    stream = useTaskEvents(id, async () => { await refreshFromEvent(id, generation) })
    return result
  }

  return {
    tasks,
    detail,
    loading,
    error,
    activeCount,
    load,
    refreshDetail,
    watchTask,
    stopWatching,
    // Compatibility aliases for callers from the polling implementation.
    startPolling: watchTask,
    stopPolling: stopWatching,
  }
})
