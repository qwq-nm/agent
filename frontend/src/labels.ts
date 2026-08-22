import type { Evidence, TaskDetail, TaskStep, ToolCall } from './types'

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
  ctf_web: 'CTF Web 题目分析',
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
  browser_snapshot: '浏览器渲染快照',
  dirsearch_scan: 'dirsearch 路径发现',
  robots_analyzer: 'robots.txt 分析',
  js_analyzer: '前端脚本分析',
  path_normalizer: '路径规范化',
  flag_pattern_detector: 'Flag 模式识别',
  cookie_analyzer: 'Cookie 安全属性分析',
  sensitive_file_checker: '敏感文件线索检查',
  report_generator: '报告生成',
}

const STEP_NAME_LABELS: Record<string, string> = {
  validate_target_url: '校验目标 URL 是否安全',
  fetch_homepage: '获取首页内容',
  retry_http_fetch: '重新获取页面内容',
  fetch_robots_txt: '获取 robots.txt 文件',
  fetch_robots: '获取 robots.txt 内容',
  fetch_login: '获取登录页面',
  analyze_homepage_headers: '分析首页响应头',
  analyze_headers: '分析响应头',
  extract_forms: '提取页面表单',
  extract_links: '提取页面链接',
  browser_snapshot: '获取浏览器渲染快照',
  render_browser_snapshot: '获取浏览器渲染快照',
  dirsearch_scan: '执行 dirsearch 路径发现',
  discover_paths: '执行 dirsearch 路径发现',
  analyze_javascript: '分析前端脚本线索',
  normalize_paths: '整理同源候选路径',
  detect_flag_patterns: '识别疑似 Flag',
  analyze_cookies: '分析 Cookie 安全属性',
  check_sensitive_files: '检查敏感文件线索',
  generate_report: '生成安全分析报告',
}

const TOOL_PURPOSES: Record<string, string> = {
  url_guard: '在真正访问目标前，检查 URL 格式、协议、主机和安全边界，防止越权访问、SSRF 或访问未授权地址。',
  http_fetch: '获取授权目标的页面内容、状态码和响应头，为后续页面结构、链接、表单、脚本和疑似 Flag 分析提供基础证据。',
  header_check: '检查 HTTP 响应头中的安全配置，识别缺失的安全头或可能影响分析的响应特征。',
  form_extract: '从页面 HTML 中提取公开表单，帮助判断页面是否存在登录、搜索、提交等可继续分析的交互入口。',
  link_extract: '从页面中提取同源公开链接，帮助发现可继续访问的路径、目录或功能页面。',
  browser_snapshot: '使用无头浏览器加载授权页面，获取 JavaScript 渲染后的可见文本、链接、表单和截图，解决普通 HTTP 抓取看不到真实页面内容的问题。',
  dirsearch_scan: '调用标准 dirsearch 外部工具，在授权目标范围内发现常见路径、目录和文件线索；结果会进入证据账本并作为后续 AI 重规划依据。',
  robots_analyzer: '分析 robots.txt 中的公开规则，寻找站点声明的目录、文件或隐藏路径线索。',
  js_analyzer: '分析前端脚本中的路由、接口、关键词和可疑路径线索，为下一步路径分析提供候选目标。',
  path_normalizer: '把页面、脚本或 robots.txt 中发现的候选路径整理成同源、规范、可审计的路径清单。',
  flag_pattern_detector: '在已获取的公开页面内容中识别疑似 Flag 或比赛题目常见标记。',
  cookie_analyzer: '分析 Set-Cookie 中的安全属性，判断是否缺少 HttpOnly、Secure、SameSite 等推荐配置。',
  sensitive_file_checker: '基于公开内容中的路径和文件名线索，识别备份文件、配置文件或敏感文件提示。',
  log_type_detector: '识别日志类型和基础格式，确定后续应该使用哪类日志分析策略。',
  log_analyzer: '解析日志字段、访问来源、状态码和异常请求，为攻击特征识别提供结构化输入。',
  attack_pattern_detector: '根据日志中的路径、参数、状态码和访问频率识别扫描、爆破或常见攻击模式。',
  timeline_builder: '把日志事件按时间顺序整理，帮助还原攻击过程和处置顺序。',
  project_detector: '识别源码项目语言、框架和目录结构，为后续源码审计选择合适规则。',
  source_scanner: '扫描源码中的危险函数、可疑输入处理和常见漏洞模式。',
  secret_scanner: '查找源码或配置中的硬编码密钥、令牌、密码等敏感信息。',
  config_checker: '检查配置文件中的调试开关、弱配置、暴露风险和不安全默认值。',
  report_generator: '汇总证据账本、工具调用和模型判断，生成可追溯的中文分析报告。',
}

const ENGLISH_TEXT_LABELS: Array<[RegExp, string]> = [
  [/^[\s\S]*Verify URL is a well-formed and safe target[\s\S]*$/i, '在访问目标前确认 URL 格式、授权范围和安全边界，避免越权访问或 SSRF 风险。'],
  [/^[\s\S]*Retrieve the homepage HTML[\s\S]*$/i, '获取首页 HTML，作为后续页面结构、链接、表单、脚本线索和疑似 Flag 分析的基础证据。'],
  [/^[\s\S]*Re-fetch the target homepage[\s\S]*$/i, '由于前一次页面获取失败，重新获取目标首页，补充后续静态分析所需的基础响应证据。'],
  [/^[\s\S]*Fetch robots\.txt content[\s\S]*$/i, '获取 robots.txt 内容，用于发现公开声明的目录、文件或隐藏路径线索。'],
  [/^[\s\S]*Fetch robots\.txt[\s\S]*$/i, '获取 robots.txt 文件，用于检查站点公开的爬虫规则和路径线索。'],
  [/^[\s\S]*Fetch the login page[\s\S]*$/i, '获取登录页面的公开 HTML，用于分析表单字段、注释和页面结构，不提交表单。'],
  [/^[\s\S]*Target URL passed SSRF policy check[\s\S]*$/i, '目标 URL 已通过 SSRF 安全边界检查。'],
  [/^[\s\S]*URL guard allowed[\s\S]*$/i, 'URL 安全边界检查通过，目标允许在当前授权范围内访问。'],
  [/^HTTP request failed before response:\s*(.+)$/i, 'HTTP 请求在获得响应前失败，可能是目标不可达、端口不通、超时或网络策略限制。错误类型：$1。'],
  [/^[\s\S]*HTTP response parameter is missing or invalid[\s\S]*$/i, '缺少有效的 HTTP 响应数据，当前工具需要先获得页面获取工具的结构化结果。'],
  [/^[\s\S]*HTTP response headers are missing or invalid[\s\S]*$/i, '缺少有效的 HTTP 响应头数据，当前工具需要先获得页面获取工具的结构化结果。'],
  [/^[\s\S]*HTTP response body preview is invalid[\s\S]*$/i, '缺少有效的页面正文预览，当前工具需要先获得页面获取工具的结构化结果。'],
  [/^[\s\S]*Tool expected response metadata from http_fetch[\s\S]*$/i, '该工具需要使用 HTTP 页面获取工具产生的结构化响应数据。'],
  [/^[\s\S]*Observed (\d+) missing security headers[\s\S]*$/i, '发现 $1 个缺失的安全响应头。'],
  [/^[\s\S]*Passively extracted (\d+) forms[\s\S]*$/i, '被动提取到 $1 个公开表单。'],
  [/^[\s\S]*Extracted (\d+) same-origin public links[\s\S]*$/i, '提取到 $1 个同源公开链接。'],
  [/^[\s\S]*Prepared robots\.txt candidate URL[\s\S]*$/i, '已生成 robots.txt 候选访问地址。'],
  [/^[\s\S]*Parsed (\d+) robots\.txt directives[\s\S]*$/i, '解析到 $1 条 robots.txt 规则。'],
  [/^[\s\S]*Found (\d+) client-side route hints and (\d+) keywords[\s\S]*$/i, '发现 $1 个前端路由线索和 $2 个关键词。'],
  [/^[\s\S]*Normalized (\d+) same-origin candidate paths[\s\S]*$/i, '整理出 $1 个同源候选路径。'],
  [/^[\s\S]*Detected (\d+) flag-like patterns[\s\S]*$/i, '发现 $1 个疑似 Flag 模式。'],
  [/^[\s\S]*No Set-Cookie header observed[\s\S]*$/i, '未观察到 Set-Cookie 响应头。'],
  [/^[\s\S]*Analyzed Set-Cookie header; missing (\d+) recommended attributes[\s\S]*$/i, '已分析 Set-Cookie 响应头，发现 $1 个推荐安全属性缺失。'],
  [/^[\s\S]*Detected (\d+) sensitive file or backup path hints[\s\S]*$/i, '发现 $1 个敏感文件或备份路径线索。'],
  [/\bas the foundational evidence for all subsequent passive analysis; required to search page content for the flag and to discover links, forms, and script hints\.?/i, '作为后续页面结构、链接、表单、脚本线索和疑似 Flag 分析的基础证据。'],
  [/\bbefore making any network request; this addresses the missing initial safety evidence and ensures the subsequent fetch is authorized\.?/i, '该步骤在访问目标前完成安全边界确认，确保后续请求仍在授权范围内。'],
  [/\bbecause the earlier HTTP request failed with http_transport_error\. This is the first step to obtain the HTTP response body, which is the core evidence required for all subsequent static analysis \(response content, headers, forms, links, robots, flag patterns\)\.?/i, '因为上一轮 HTTP 请求失败，本步骤用于重新获取页面正文和响应头，补齐后续静态分析需要的基础证据。'],
]

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

export function modelStageActionLabel(value?: string | null) {
  if (value === 'task_parse') return '读取用户目标、授权范围、目标 URL 和安全策略，判断任务属于哪类安全分析场景。'
  if (value === 'plan') return '根据当前任务和已有证据选择下一组白名单工具，并给出步骤执行依据。'
  if (value === 'critic') return '检查工具输出是否足够支撑结论；如果证据不足，会要求继续补充或重新规划。'
  if (value === 'report') return '只引用证据账本和工具调用记录，整理成可追溯的中文分析报告。'
  return '根据当前上下文进行判断，并把决策结果交给系统执行或记录。'
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

export function translateKnownText(value?: string | null) {
  if (!value) return ''
  for (const [pattern, label] of ENGLISH_TEXT_LABELS) {
    if (pattern.test(value)) return value.replace(pattern, label)
  }
  return value
}

export function stepNameLabel(step?: Pick<TaskStep, 'name' | 'tool_name'> | Pick<ToolCall, 'step_name' | 'tool_name'> | null) {
  if (!step) return '未命名步骤'
  const rawName = Object.prototype.hasOwnProperty.call(step, 'step_name')
    ? (step as Pick<ToolCall, 'step_name' | 'tool_name'>).step_name
    : (step as Pick<TaskStep, 'name' | 'tool_name'>).name
  if (rawName && STEP_NAME_LABELS[rawName]) return STEP_NAME_LABELS[rawName]
  if (rawName && /^[A-Za-z0-9_-]+$/.test(rawName) && step.tool_name) return toolNameLabel(step.tool_name)
  return translateKnownText(rawName || toolNameLabel(step.tool_name))
}

export function toolPurposeLabel(toolName?: string | null, fallback?: string | null) {
  const translated = translateKnownText(fallback)
  if (translated && translated !== fallback) return translated
  if (toolName && TOOL_PURPOSES[toolName]) return TOOL_PURPOSES[toolName]
  return translated || '这一步用于补充当前分析所需证据，工具输出会进入证据账本，供 AI 后续复核或重新规划使用。'
}

export function toolCapabilityLabel(toolName?: string | null) {
  if (toolName && TOOL_PURPOSES[toolName]) return TOOL_PURPOSES[toolName]
  return '该工具用于补充当前任务所需的结构化事实，输出会写入证据账本。'
}

export function toolCallReasonLabel(call: Pick<ToolCall, 'tool_name' | 'step_purpose' | 'result'>) {
  const translated = translateKnownText(call.step_purpose)
  const capability = toolCapabilityLabel(call.tool_name)
  if (translated && translated !== capability) return translated
  const error = resultErrorCode(call.result)
  if (error === 'invalid_http_response') return '前置页面响应缺失，系统尝试执行该步骤以验证证据链是否完整。'
  if (error === 'http_transport_error' || error === 'http_timeout') return '需要获取目标页面作为基础证据，但本次网络请求未成功返回。'
  return '根据当前场景策略和已有证据缺口，系统选择该白名单工具补充事实。'
}

function resultErrorCode(result?: Record<string, unknown> | null) {
  const direct = result?.error
  if (typeof direct === 'string' && direct) return direct
  const fallback = result?.error_code
  if (typeof fallback === 'string' && fallback) return fallback
  return ''
}

function resultEvidence(result?: Record<string, unknown> | null) {
  const evidence = result?.evidence
  return Array.isArray(evidence) ? evidence.filter((item): item is Record<string, unknown> => item !== null && typeof item === 'object') : []
}

function resultFindings(result?: Record<string, unknown> | null) {
  const findings = result?.findings
  return Array.isArray(findings) ? findings.filter((item): item is Record<string, unknown> => item !== null && typeof item === 'object') : []
}

function firstMetadata(result?: Record<string, unknown> | null) {
  const item = resultEvidence(result)[0]
  const metadata = item?.metadata
  return metadata && typeof metadata === 'object' ? metadata as Record<string, unknown> : {}
}

export function toolResultSummaryLabel(result?: Record<string, unknown> | null) {
  const error = resultErrorCode(result)
  if (error) return translateKnownText(`HTTP request failed before response: ${error}`)
  const summary = result?.summary
  if (typeof summary === 'string' && summary) return translateKnownText(summary)
  return '暂无可读摘要，请查看工具输出详情。'
}

export function toolExecutionResultLabel(toolName?: string | null, result?: Record<string, unknown> | null) {
  const error = resultErrorCode(result)
  if (error) return toolResultSummaryLabel(result)
  const metadata = firstMetadata(result)
  const findings = resultFindings(result)
  const summary = toolResultSummaryLabel(result)
  if (toolName === 'http_fetch') {
    const status = metadata.status_code
    const finalUrl = metadata.final_url
    const headers = metadata.headers
    const body = metadata.body_preview
    const headerCount = headers && typeof headers === 'object' ? Object.keys(headers).length : 0
    const bodyLength = typeof body === 'string' ? body.length : 0
    if (typeof status === 'number' || typeof status === 'string') {
      return `已获得 HTTP ${status} 响应；目标：${String(finalUrl || '未记录')}；响应头 ${headerCount} 个；页面正文预览约 ${bodyLength} 字。`
    }
  }
  if (toolName === 'header_check') {
    const headers = findings.map((item) => item.header).filter(Boolean).slice(0, 6).join('、')
    return headers ? `发现 ${findings.length} 个缺失或需关注的安全响应头：${headers}。` : summary
  }
  if (toolName === 'link_extract') {
    const sample = findings.map((item) => item.url).filter(Boolean).slice(0, 3).join('、')
    return sample ? `提取到 ${findings.length} 个同源公开链接，示例：${sample}。` : summary
  }
  if (toolName === 'form_extract') {
    return findings.length ? `提取到 ${findings.length} 个公开表单，可继续分析表单方法、字段和 action。` : '未发现公开表单。'
  }
  if (toolName === 'flag_pattern_detector') {
    const sample = findings.map((item) => item.pattern).filter(Boolean).slice(0, 3).join('、')
    return sample ? `发现 ${findings.length} 个疑似 Flag：${sample}。` : '未在当前公开内容中发现疑似 Flag。'
  }
  return summary
}

export function stepStatusExplanationLabel(step: TaskStep, call?: ToolCall) {
  const error = resultErrorCode(call?.result)
  if (step.status === 'success') return `该步骤已完成。${toolExecutionResultLabel(step.tool_name, call?.result)}`
  if (step.status === 'failed' || step.status === 'failed_retryable') {
    if (error === 'http_transport_error') return '该步骤失败：HTTP 请求在获得响应前中断，常见原因是目标不可达、端口不通、连接被拒绝或容器网络受限。'
    if (error === 'http_timeout') return '该步骤失败：HTTP 请求超时，目标可能响应过慢或临时不可达。'
    if (error === 'invalid_http_response') return '该步骤失败：缺少前置 HTTP 响应数据，说明依赖页面内容的后续分析没有拿到输入。'
    return `该步骤失败：${toolResultSummaryLabel(call?.result)}`
  }
  if (step.status === 'running') return '系统正在执行该步骤，完成后会把工具输出写入证据账本。'
  if (step.status === 'pending') return '该步骤尚未执行，通常在等待前置步骤、调度队列或人工确认。'
  return '系统已记录此步骤状态，后续判断会参考它的执行结果。'
}

export function stepImpactExplanationLabel(step: TaskStep, call?: ToolCall) {
  const error = resultErrorCode(call?.result)
  if (error === 'http_transport_error' || error === 'http_timeout') {
    return '影响：没有拿到页面正文和响应头，链接提取、表单提取、脚本分析和疑似 Flag 检测会缺少基础输入。'
  }
  if (error === 'invalid_http_response') {
    return '影响：该工具依赖 http_fetch 的结构化结果，因此本轮不能产生有效结论，后续应优先补齐页面响应证据。'
  }
  if (step.status === 'success') {
    return `影响：${toolExecutionResultLabel(step.tool_name, call?.result)} 该结果会进入证据链，供 AI 复核、重规划或报告引用。`
  }
  return '影响：该步骤尚未形成可用证据，后续需要等待执行结果或重新规划。'
}

export function taskFailureReasonLabel(task: TaskDetail) {
  const failedStep = [...task.steps].reverse().find((step) => step.status === 'failed' || step.status === 'failed_retryable')
  const failedCall = [...task.tool_calls].reverse().find((call) => {
    const error = resultErrorCode(call.result)
    return error || call.status === 'failed'
  })
  const latestModelError = [...task.model_calls].reverse().find((call) => call.status === 'error' || call.error_code)
  if (failedCall) {
    const error = resultErrorCode(failedCall.result)
    return {
      title: `失败位置：${stepNameLabel(failedCall)}`,
      reason: error ? toolResultSummaryLabel(failedCall.result) : '工具调用没有产生可用结果。',
      suggestion: error === 'invalid_http_response'
        ? '建议先确认 http_fetch 是否成功，或重新生成计划，让后续工具使用最新页面响应。'
        : error === 'http_transport_error' || error === 'http_timeout'
          ? '建议检查目标 URL、端口、容器网络连通性，或稍后重试。'
          : '建议查看工具输出详情，确认是否需要补充工具能力或重新规划。',
    }
  }
  if (latestModelError) {
    return {
      title: `失败位置：${stageLabel(latestModelError.stage)}`,
      reason: `模型调用失败：${errorCodeLabel(latestModelError.error_code)}。`,
      suggestion: '建议检查 API Key、模型路由、额度、网络和 provider 连通性。',
    }
  }
  if (task.status === 'failed' || task.status === 'failed_retryable') {
    return {
      title: failedStep ? `失败位置：${stepNameLabel(failedStep)}` : '任务已停止',
      reason: '系统未记录到更具体的工具或模型错误，可能是重规划次数、任务时间或步骤上限耗尽。',
      suggestion: '建议查看完整运行记录和 Docker worker 日志，确认是否达到 max_replans、timeout 或 max_steps 限制。',
    }
  }
  return null
}

export function evidenceContentLabel(item: Pick<Evidence, 'content'>) {
  return translateKnownText(item.content)
}

export function evidenceSourceLabel(source?: string | null) {
  if (!source) return '未知来源'
  return source
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
    summary.push(`正在执行“${stepNameLabel(runningStep)}”，目标是：${toolPurposeLabel(runningStep.tool_name, runningStep.purpose)}。`)
  } else if (pendingStep) {
    summary.push(`下一步等待执行“${stepNameLabel(pendingStep)}”，将调用“${toolNameLabel(pendingStep.tool_name)}”。`)
  } else if (latestStep) {
    summary.push(`最近完成的步骤是“${stepNameLabel(latestStep)}”，使用了“${toolNameLabel(latestStep.tool_name)}”。`)
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
