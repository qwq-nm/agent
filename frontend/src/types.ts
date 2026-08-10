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

export interface TaskStep {
  id: string
  index?: number
  name: string
  purpose?: string
  status: string
  model_provider?: string
  tool_name?: string
  route_reason?: string
  risk_level?: string
}

export interface Evidence {
  id: string
  evidence_type: string
  source: string
  content: string
  confidence: number
  metadata?: Record<string, unknown>
}

export interface ModelCall {
  id: string
  provider: string
  model: string
  stage: string
  route_reason: string
  latency_ms: number
  is_demo: boolean
}

export interface ToolCall {
  id: string
  tool_name: string
  params: Record<string, unknown>
  result: Record<string, unknown>
  status: string
}

export interface PendingApproval {
  step_id: string
  tool_name: string
  risk_level: string
  params_summary: string
}

export interface TaskReport {
  id: string
  content: string
  is_demo: boolean
}

export interface TaskDetail extends Task {
  steps: TaskStep[]
  evidences: Evidence[]
  model_calls: ModelCall[]
  tool_calls: ToolCall[]
  reports: TaskReport[]
  pending_approval?: PendingApproval | null
}

export interface ModelStatus {
  name: string
  configured: boolean
  mode: string
}

export interface ToolStatus {
  name: string
  scene: string
  risk_level: string
}
