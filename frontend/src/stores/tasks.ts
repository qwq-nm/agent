import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api } from '../api/client'
import type { Task, TaskDetail } from '../types'

export const useTasksStore = defineStore('tasks', () => {
  const tasks = ref<Task[]>([])
  const loading = ref(false)
  const error = ref('')
  const detail = ref<TaskDetail>()
  let pollTimer: ReturnType<typeof setTimeout> | undefined
  let pollingId: string | undefined

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

  function stopPolling() {
    pollingId = undefined
    if (pollTimer) clearTimeout(pollTimer)
    pollTimer = undefined
  }

  async function refreshDetail(id: string) {
    detail.value = await api.getTask(id)
    if (pollingId === id && detail.value.status === 'running') {
      pollTimer = setTimeout(() => void refreshDetail(id), 2000)
    } else if (pollingId === id) {
      stopPolling()
    }
    return detail.value
  }

  async function startPolling(id: string) {
    stopPolling()
    pollingId = id
    return refreshDetail(id)
  }

  return {
    tasks,
    detail,
    loading,
    error,
    activeCount,
    load,
    refreshDetail,
    startPolling,
    stopPolling,
  }
})
