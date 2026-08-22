export type TaskStatus =
  | 'created'
  | 'planning'
  | 'queued'
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

export type SafetyMode = 'conservative' | 'standard' | 'expert'

export type UserRole = 'admin' | 'analyst'

export type ProviderName = 'deepseek' | 'glm'

export interface ProviderCredential {
  provider: ProviderName
  configured: boolean
  key_hint?: string | null
  updated_at?: string | null
}

export interface ProviderRouteOption {
  provider: string
  route: string
  display_name: string
  base_url: string
  model: string
  api_style: string
  reasoning_effort?: string | null
}

export interface ProviderRoute extends ProviderRouteOption {
  configured: boolean
  updated_at?: string | null
}

export interface AuthUser {
  id: string
  username: string
  role: UserRole
}

export interface AuthResponse {
  access_token: string
  token_type?: 'bearer'
  expires_in?: number
  user: AuthUser
}

export interface Task {
  id: string
  goal: string
  authorization_scope: string
  route_mode: RouteMode
  safety_mode: SafetyMode
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
  safety_mode: SafetyMode
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

export interface PlanPreviewStep {
  index: number
  name: string
  purpose?: string
  tool_name?: string
  params?: Record<string, unknown>
  risk_level?: string
  need_human_confirm?: boolean
}

export interface PlanPreview {
  task_id: string
  scene?: string
  goal_summary: string
  target_summary?: string | null
  authorization_summary: string
  safety_mode: SafetyMode
  constraints: string[]
  expected_outputs: string[]
  steps: PlanPreviewStep[]
}

export interface Evidence {
  id: string
  evidence_type: string
  source: string
  content: string
  confidence: number
  metadata?: Record<string, unknown>
  evidence_hash?: string
}

export interface ModelCall {
  id: string
  provider: string
  model: string
  stage: string
  route_reason: string
  latency_ms: number
  is_demo: boolean
  attempt?: number
  input_tokens?: number
  output_tokens?: number
  prompt_tokens?: number
  completion_tokens?: number
  retry_count?: number
  request_id?: string | null
  error_code?: string | null
  status?: string
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
  evidence_ids?: string[]
  is_demo: boolean
}

export interface TaskDetail extends Task {
  steps: TaskStep[]
  evidences: Evidence[]
  model_calls: ModelCall[]
  tool_calls: ToolCall[]
  reports: TaskReport[]
  plan_preview?: PlanPreview | null
  pending_approval?: PendingApproval | null
  queue_position?: number | null
  job_attempt?: number | null
  worker_id?: string | null
  worker_heartbeat_at?: string | null
  heartbeat_at?: string | null
  current_stage?: string | null
}

export interface ModelStatus {
  name: string
  display_name?: string
  configured: boolean
  mode: string
  model?: string
  status?: string
  error_code?: string | null
  api_style?: string | null
  base_url?: string | null
}

export interface WorkerSummary {
  online: number
  active: number
  capacity: number
  queued: number
}

export interface ReadinessStatus {
  status: string
  checks: Record<string, string>
}

export interface ProviderCheck {
  provider: string
  model: string
  status: string
  request_id?: string | null
  input_tokens?: number | null
  output_tokens?: number | null
  latency_ms?: number | null
  error_code?: string | null
}

export interface AdminUser {
  id: string
  username: string
  role: UserRole
  is_active: boolean
}

export interface AuditEvent {
  id: number
  actor_id?: string | null
  actor_username?: string | null
  action: string
  resource_type: string
  resource_id?: string | null
  outcome: string
  details?: Record<string, unknown>
  created_at: string
}

export interface ToolStatus {
  name: string
  scene: string
  risk_level: string
}
