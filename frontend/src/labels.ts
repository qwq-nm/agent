import type { TaskDetail } from './types'

const STATUS_LABELS: Record<string, string> = {
  created: '已创建',
  planning: '生成计划中',
  queued: '排队中',
  parsed: '已理解任务',
  planned: '已生成计划',
  running: '执行中',
  waiting_human: '等待人工确认',
  paused: '已暂停',
  completed: '已完成',
  failed_retryable: '失败，可重试',
  failed: '失败',
  cancelled: '已取消',
  pending: '等待执行',
  success: '成功',
  failed_retryable_step: '失败，可重试',
}

const SCENE_LABELS: Record<string, string> = {
  auto: '自动判断',
  web_analysis: 'Web 被动分析',
  incident_response: '日志应急分析',
  source_audit: '源码安全审计',
  policy_review: '策略与风险复核',
}

const STAGE_LABELS: Record<string, string> = {
  task_parse: '任务理解',
  plan: '计划生成',
  critic: '证据复核',
  report: '报告生成',
  unknown: '未知阶段',
}

const ROUTE_REASON_LABELS: Record<string, string> = {
  中文任务理解: '中文任务理解',
  技术计划生成: '技术计划生成',
  证据完整性复核: '证据完整性复核',
  中文报告生成: '中文报告生成',
  fixed_stage: '固定流程节点',
}

const RISK_LABELS: Record<string, string> = {
  low: '低风险',
  medium: '中风险，需要确认',
  high: '高风险，需要严格确认',
}

const SAFETY_MODE_LABELS: Record<string, string> = {
  conservative: '保守模式',
  standard: '标准模式',
  expert: '专家模式',
}

const EVIDENCE_TYPE_LABELS: Record<string, string> = {
  http_response: 'HTTP 响应',
  http_headers: '响应头',
  html_form: '页面表单',
  link_inventory: '链接清单',
  robots_rules: 'robots.txt 规则',
  js_hint: '前端脚本线索',
  normalized_path: '规范化路径',
  flag_candidate: '疑似 Flag',
  raw_line: '原始日志行',
  log_summary: '日志摘要',
  attack_pattern: '攻击模式',
  source_file: '源码文件',
  secret_candidate: '疑似敏感信息',
  runtime_error: '运行错误',
  observation: '观察结果',
  note: '说明',
}

const TOOL_LABELS: Record<string, string> = {
  demo_evidence: '演示证据生成',
  log_type_detector: '日志类型识别',
  log_analyzer: '日志字段分析',
  attack_pattern_detector: '攻击模式检测',
  timeline_builder: '事件时间线构建',
  project_detector: '项目类型识别',
  source_scanner: '源码风险扫描',
  secret_scanner: '敏感信息扫描',
  config_checker: '配置风险检查',
  url_guard: 'URL 安全边界检查',
  http_fetch: 'HTTP 页面获取',
  header_check: '响应头安全检查',
  form_extract: '表单提取',
  link_extract: '链接提取',
  robots_analyzer: 'robots.txt 分析',
  js_analyzer: '前端脚本分析',
  path_normalizer: '路径规范化',
  flag_pattern_detector: 'Flag 模式识别',
  cookie_analyzer: 'Cookie 安全属性分析',
  sensitive_file_checker: '敏感文件线索检查',
  report_generator: '报告生成',
}

const ERROR_LABELS: Record<string, string> = {
  auth: '鉴权失败，请检查 API Key',
  rate_limit: '限流或额度不足',
  server: '模型服务异常',
  timeout: '请求超时',
  network: '网络连接失败',
  empty_content: '模型返回为空',
  truncated: '模型输出被截断',
  invalid_json: '模型没有返回合法 JSON',
  invalid_schema: '模型返回结构不符合要求',
}

export function statusLabel(value?: string | null) {
  if (!value) return '未知状态'
  return STATUS_LABELS[value] || value
}

export function sceneLabel(value?: string | null) {
  if (!value) return '场景待识别'
  return SCENE_LABELS[value] || value
}

export function stageLabel(value?: string | null) {
  if (!value) return '策略/工具'
  return STAGE_LABELS[value] || value
}

export function routeReasonLabel(value?: string | null) {
  if (!value) return '按场景策略执行'
  return ROUTE_REASON_LABELS[value] || value
}

export function riskLabel(value?: string | null) {
  if (!value) return '低风险'
  return RISK_LABELS[value] || value
}

export function safetyModeLabel(value?: string | null) {
  if (!value) return '保守模式'
  return SAFETY_MODE_LABELS[value] || value
}

export function evidenceTypeLabel(value?: string | null) {
  if (!value) return '观察结果'
  return EVIDENCE_TYPE_LABELS[value] || value
}

export function toolNameLabel(value?: string | null) {
  if (!value) return '未调用工具'
  return TOOL_LABELS[value] || value
}

export function providerName(value?: string | null) {
  if (!value) return '策略/工具'
  if (value.toLowerCase() === 'deepseek') return 'DeepSeek'
  if (value.toLowerCase() === 'glm') return 'GLM'
  if (value.toLowerCase() === 'mock') return 'Mock 演示模型'
  return value
}

export function errorCodeLabel(value?: string | null) {
  if (!value) return '无'
  return ERROR_LABELS[value] || value
}

export function confidenceLabel(value: number) {
  if (value >= 0.85) return '高'
  if (value >= 0.6) return '中'
  return '低'
}

export function summarizeTaskDecision(task: TaskDetail) {
  const completedSteps = task.steps.filter((step) => step.status === 'success').length
  const runningStep = task.steps.find((step) => step.status === 'running')
  const pendingStep = task.steps.find((step) => step.status === 'pending')
  const latestStep = [...task.steps].reverse().find((step) => step.status !== 'pending')
  const latestModel = task.model_calls.at(-1)
  const latestEvidence = task.evidences.at(-1)

  const summary = [
    `系统当前处于“${statusLabel(task.status)}”状态，任务场景是“${sceneLabel(task.scene || task.scene_hint)}”，安全策略是“${safetyModeLabel(task.safety_mode)}”。`,
    `已执行 ${completedSteps}/${task.steps.length} 个步骤，证据账本中已有 ${task.evidences.length} 条证据。`,
  ]

  if (runningStep) {
    summary.push(`正在执行“${runningStep.name}”，目标是：${runningStep.purpose || '补充当前任务所需证据'}。`)
  } else if (pendingStep) {
    summary.push(`下一步等待执行“${pendingStep.name}”，将调用“${toolNameLabel(pendingStep.tool_name)}”。`)
  } else if (latestStep) {
    summary.push(`最近完成的步骤是“${latestStep.name}”，使用了“${toolNameLabel(latestStep.tool_name)}”。`)
  }

  if (latestModel) {
    summary.push(
      `最近一次模型节点是“${stageLabel(latestModel.stage)}”，作用是“${routeReasonLabel(latestModel.route_reason)}”。`,
    )
  }

  if (latestEvidence) {
    summary.push(
      `最新证据来自“${latestEvidence.source}”，类型是“${evidenceTypeLabel(latestEvidence.evidence_type)}”，置信度为 ${latestEvidence.confidence.toFixed(2)}。`,
    )
  }

  if (task.status === 'waiting_human') {
    summary.push('当前卡点是人工确认：只有批准当前中风险或高风险工具后，后续步骤才会继续。')
  }
  if (task.status === 'failed_retryable') {
    summary.push('当前失败属于可重试状态：通常是模型、网络、临时工具错误或证据不足导致，可以点击重试继续。')
  }
  if (task.status === 'failed') {
    summary.push('当前失败不可自动继续：请优先查看证据账本、模型调用错误和已生成的阶段性报告。')
  }
  if (task.status === 'completed') {
    summary.push('系统已经停止自动执行，并生成或保存了可追溯的分析结果。')
  }

  return summary
}
