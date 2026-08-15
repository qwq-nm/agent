import type {
  AdminUser,
  AuditEvent,
  HealthStatus,
  ModelStatus,
  ProviderCheck,
  ProviderCredential,
  ProviderName,
  ReadinessStatus,
  Task,
  TaskCreate,
  TaskDetail,
  ToolStatus,
  WorkerSummary,
} from '../types'
import { apiRequest, apiTextRequest } from './http'

export async function request<T>(url: string, init?: RequestInit): Promise<T> {
  return apiRequest<T>(url, init)
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
  workers: () => request<WorkerSummary>('/api/admin/workers'),
  readiness: () => request<ReadinessStatus>('/api/health/ready'),
  providerCheck: (provider: string) => request<ProviderCheck>('/api/models/check', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ provider }),
  }),
  listProviderCredentials: () => request<ProviderCredential[]>('/api/admin/provider-credentials'),
  saveProviderCredential: (provider: ProviderName, apiKey: string) =>
    request<ProviderCredential>(`/api/admin/provider-credentials/${provider}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ api_key: apiKey }),
    }),
  clearProviderCredential: async (provider: ProviderName): Promise<void> => {
    await request<ProviderCredential>(`/api/admin/provider-credentials/${provider}`, { method: 'DELETE' })
  },
  listUsers: () => request<AdminUser[]>('/api/admin/users'),
  createUser: (payload: { username: string; password: string; role: 'admin' | 'analyst' }) =>
    request<AdminUser>('/api/admin/users', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  updateUser: (id: string, payload: { is_active?: boolean; password?: string }) =>
    request<AdminUser>(`/api/admin/users/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  auditEvents: (params: { limit?: number; before?: number; actor?: string; action?: string; outcome?: string; created_after?: string; created_before?: string } = {}) => {
    const query = new URLSearchParams()
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== '') query.set(key, String(value))
    }
    return request<AuditEvent[]>(`/api/admin/audit-events${query.size ? `?${query}` : ''}`)
  },
}

export const lifecycle = {
  run: (id: string, idempotencyKey: string) => request(`/api/tasks/${id}/run`, { method: 'POST', headers: { 'Idempotency-Key': idempotencyKey } }),
  pause: (id: string) => request(`/api/tasks/${id}/pause`, { method: 'POST' }),
  resume: (id: string, idempotencyKey: string) => request(`/api/tasks/${id}/resume`, { method: 'POST', headers: { 'Idempotency-Key': idempotencyKey } }),
  retry: (id: string, idempotencyKey: string) => request(`/api/tasks/${id}/retry`, { method: 'POST', headers: { 'Idempotency-Key': idempotencyKey } }),
  cancel: (id: string, idempotencyKey: string) => request(`/api/tasks/${id}/cancel`, { method: 'POST', headers: { 'Idempotency-Key': idempotencyKey } }),
  approve: (id: string, approved: boolean, reason: string, idempotencyKey: string) =>
    request(`/api/tasks/${id}/approve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey },
      body: JSON.stringify({ approved, reason }),
    }),
  report: (id: string) => apiTextRequest(`/api/tasks/${id}/report`),
}
