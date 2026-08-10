import type {
  HealthStatus,
  ModelStatus,
  Task,
  TaskCreate,
  TaskDetail,
  ToolStatus,
} from '../types'

export async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  if (!response.ok) {
    throw new Error((await response.text()) || `HTTP ${response.status}`)
  }
  return response.json() as Promise<T>
}

export const api = {
  createTask: (payload: TaskCreate, file?: File) => {
    if (!file) {
      return request<Task>('/api/tasks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
    }
    const body = new FormData()
    body.set('payload', JSON.stringify(payload))
    body.set('file', file)
    return request<Task>('/api/tasks', { method: 'POST', body })
  },
  listTasks: () => request<Task[]>('/api/tasks'),
  getTask: (id: string) => request<TaskDetail>(`/api/tasks/${id}`),
  health: () => request<HealthStatus>('/api/health'),
  modelStatus: () => request<ModelStatus[]>('/api/models/status'),
  toolStatus: () => request<ToolStatus[]>('/api/tools'),
}

export const lifecycle = {
  run: (id: string) => request(`/api/tasks/${id}/run`, { method: 'POST' }),
  pause: (id: string) => request(`/api/tasks/${id}/pause`, { method: 'POST' }),
  resume: (id: string) => request(`/api/tasks/${id}/resume`, { method: 'POST' }),
  retry: (id: string) => request(`/api/tasks/${id}/retry`, { method: 'POST' }),
  cancel: (id: string) => request(`/api/tasks/${id}/cancel`, { method: 'POST' }),
  approve: (id: string, approved: boolean, reason: string) =>
    request(`/api/tasks/${id}/approve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ approved, reason }),
    }),
  report: async (id: string) => {
    const response = await fetch(`/api/tasks/${id}/report`)
    if (!response.ok) throw new Error(await response.text())
    return response.text()
  },
}
