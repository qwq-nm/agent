export type TaskStatus =
  | 'created'
  | 'parsed'
  | 'planned'
  | 'running'
  | 'waiting_human'
  | 'paused'
  | 'completed'
  | 'failed_retryable'
  | 'failed'
  | 'cancelled'

export type RouteMode = 'auto' | 'manual'

export interface Task {
  id: string
  goal: string
  authorization_scope: string
  route_mode: RouteMode
  preferred_model?: string
  scene_hint?: string
  target_url?: string
  scene?: string
  status: TaskStatus
  is_demo: boolean
}

export interface TaskCreate {
  goal: string
  authorization_scope: string
  route_mode: RouteMode
  preferred_model?: string
  scene_hint?: string
  target_url?: string
}

export interface HealthStatus {
  status: string
  service: string
}
