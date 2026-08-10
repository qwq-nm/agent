import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api } from '../api/client'
import type { Task } from '../types'

export const useTasksStore = defineStore('tasks', () => {
  const tasks = ref<Task[]>([])
  const loading = ref(false)
  const error = ref('')

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

  return { tasks, loading, error, activeCount, load }
})
