import type { HealthStatus, Task, TaskCreate } from '../types'

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
  getTask: (id: string) => request<Task>(`/api/tasks/${id}`),
  health: () => request<HealthStatus>('/api/health'),
}
