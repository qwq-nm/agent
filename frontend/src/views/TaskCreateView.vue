<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api/client'
import type { RouteMode, SafetyMode } from '../types'

interface MissionTemplate {
  id: string
  title: string
  scene: '' | 'ctf_web' | 'web_analysis' | 'incident_response' | 'source_audit'
  description: string
  prompt: string
  targetHint: string
}

const router = useRouter()
const submitting = ref(false)
const error = ref('')
const upload = ref<File>()
const showAdvanced = ref(false)
const safetyMode = ref<SafetyMode>('conservative')
const selectedTemplateId = ref('auto')
const form = reactive({
  goal: '',
  authorization_scope: '',
  route_mode: 'auto' as RouteMode,
  preferred_model: '',
  scene_hint: '',
  target_url: '',
})

const templates: MissionTemplate[] = [
  {
    id: 'auto',
    title: '自主综合研判',
    scene: '',
    description: '让系统自动识别任务类型，并编排合适的工具链。',
    targetHint: '适合：不确定是日志、源码还是 Web 的综合材料',
    prompt:
      '请作为通用网络安全智能体，对我提供的目标或附件进行自主研判。先理解任务类型和授权边界，再选择合适的工具链，记录证据、解释决策理由，并生成中文证据报告。',
  },
  {
    id: 'web',
    title: 'CTF / Web 被动分析',
    scene: 'ctf_web',
    description: '分析公开页面、响应头、表单、链接、JS、robots.txt 与疑似 flag。',
    targetHint: '适合：授权 CTF Web 题、靶场 Web 页面',
    prompt:
      '对授权 CTF Web 题进行被动分析。只允许 GET/HEAD 请求访问公开页面，包括首页、robots.txt、公开链接、JS/CSS 静态文件和登录页。分析页面结构、响应头、公开表单、隐藏路径线索和可能的 flag 线索，生成中文证据报告。不要提交表单，不要爆破，不要进行破坏性操作。',
  },
  {
    id: 'log',
    title: '日志应急分析',
    scene: 'incident_response',
    description: '识别日志类型、异常访问、攻击特征、时间线和处置建议。',
    targetHint: '适合：access.log、auth.log、安全设备日志',
    prompt:
      '分析上传的日志文件，识别日志类型、关键字段、异常请求、攻击模式、可疑 IP、时间线和影响范围。所有结论都要引用证据账本，最后生成中文应急分析报告。',
  },
  {
    id: 'source',
    title: '源码静态审计',
    scene: 'source_audit',
    description: '识别项目类型、危险函数、配置风险、硬编码密钥和可疑入口。',
    targetHint: '适合：源码目录、ZIP 包、小型 Web 项目',
    prompt:
      '对上传的源码或 ZIP 项目进行静态安全审计。识别项目类型、入口文件、配置风险、危险函数、敏感信息、鉴权薄弱点和可能的漏洞位置。只做静态读取，不执行项目代码，生成中文证据报告。',
  },
  {
    id: 'policy',
    title: '策略与风险复核',
    scene: '',
    description: '复核已有分析材料、工具输出或报告，判断证据是否充分。',
    targetHint: '适合：已有扫描结果、初步报告、比赛演示材料',
    prompt:
      '复核我提供的安全分析材料，检查证据是否充分、结论是否能回溯、风险判断是否合理，并指出还缺少哪些证据。请输出中文复核报告。',
  },
]

const safetyDescriptions: Record<SafetyMode, string> = {
  conservative: '只允许低风险、被动、只读工具；中高风险动作必须人工确认。',
  standard: '允许授权范围内的常规分析工具；中高风险动作需要人工确认。',
  expert: '允许更主动的授权测试流程；危险动作仍需人工确认和审计记录。',
}

const selectedTemplate = computed(() =>
  templates.find((item) => item.id === selectedTemplateId.value) || templates[0],
)

const targetUrlFromGoal = computed(() => {
  const match = form.goal.match(/https?:\/\/[^\s，。；;]+/i)
  return match?.[0] || ''
})

const effectiveTargetUrl = computed(() => form.target_url || targetUrlFromGoal.value)

const generatedAuthorization = computed(() => {
  const target = effectiveTargetUrl.value
  const fileScope = upload.value
    ? `可以读取本次上传的文件“${upload.value.name}”。`
    : '如上传附件，仅允许读取本次任务工作区内的附件。'
  const targetScope = target
    ? `仅允许访问授权目标 ${target} 及其同源公开路径。`
    : '仅允许围绕用户描述的授权目标或上传材料进行分析。'
  return [
    targetScope,
    fileScope,
    safetyDescriptions[safetyMode.value],
    '禁止越权访问、爆破、破坏性操作、端口扫描、非授权外联和真实漏洞利用。',
    '所有工具调用必须记录到证据账本，报告结论必须能回溯到证据。',
  ].join('')
})

const canSubmit = computed(() => form.goal.trim().length >= 3)

function applyTemplate(template: MissionTemplate) {
  selectedTemplateId.value = template.id
  form.scene_hint = template.scene
  if (!form.goal.trim()) form.goal = template.prompt
}

async function submit() {
  if (!canSubmit.value || submitting.value) return
  submitting.value = true
  error.value = ''
  try {
    const task = await api.createTask(
      {
        ...form,
        authorization_scope: form.authorization_scope.trim() || generatedAuthorization.value,
        safety_mode: safetyMode.value,
        preferred_model: form.preferred_model || undefined,
        scene_hint: form.scene_hint || undefined,
        target_url: effectiveTargetUrl.value || undefined,
      },
      upload.value,
    )
    await api.planTask(task.id)
    await router.push(`/tasks/${task.id}`)
  } catch (value) {
    error.value = value instanceof Error ? value.message : '生成执行计划失败'
  } finally {
    submitting.value = false
  }
}

function chooseFile(event: Event) {
  upload.value = (event.target as HTMLInputElement).files?.[0]
}
</script>

<template>
  <section class="page narrow-page">
    <header class="page-header">
      <div>
        <p class="eyebrow">NEW AGENT MISSION</p>
        <h1>创建智能体任务</h1>
        <p>用自然语言说明目标、授权范围和限制条件，系统会自动理解任务、规划步骤、选择工具并记录证据。</p>
      </div>
      <span class="safety-chip">人机协同审计</span>
    </header>

    <form class="mission-form agent-create panel" :class="{ 'is-submitting': submitting }" @submit.prevent="submit">
      <label class="agent-prompt">
        <span>你想让 SecAgent-X 做什么？</span>
        <textarea
          v-model="form.goal"
          data-test="goal"
          required
          minlength="3"
          :disabled="submitting"
          placeholder="输入目标、授权范围、限制条件即可。例如：分析这个 CTF Web 题，只允许 GET/HEAD 访问公开页面，找出页面结构、响应头、表单、隐藏路径和疑似 flag，生成中文证据报告。"
        />
      </label>

      <div class="quick-actions">
        <label class="ghost-button upload-button" :class="{ disabled: submitting }">
          <input aria-label="上传材料" type="file" :disabled="submitting" @change="chooseFile">
          {{ upload?.name || '上传附件' }}
        </label>
        <input
          v-model="form.target_url"
          class="quick-url"
          type="url"
          :disabled="submitting"
          placeholder="粘贴授权 URL，可选"
        >
        <button type="button" class="ghost-button" :disabled="submitting" @click="showAdvanced = !showAdvanced">
          {{ showAdvanced ? '收起高级设置' : '高级设置' }}
        </button>
      </div>

      <section class="template-grid" aria-label="任务模板">
        <button
          v-for="template in templates"
          :key="template.id"
          type="button"
          class="template-card"
          :class="{ active: selectedTemplate.id === template.id }"
          :disabled="submitting"
          @click="applyTemplate(template)"
        >
          <strong>{{ template.title }}</strong>
          <span>{{ template.description }}</span>
          <small>{{ template.targetHint }}</small>
        </button>
      </section>

      <section class="mode-row">
        <div>
          <span>任务模式</span>
          <div class="segmented">
            <button type="button" :disabled="submitting" :class="{ active: form.scene_hint === '' }" @click="form.scene_hint = ''">自动判断</button>
            <button type="button" :disabled="submitting" :class="{ active: form.scene_hint === 'ctf_web' }" @click="form.scene_hint = 'ctf_web'">CTF Web</button>
            <button type="button" :disabled="submitting" :class="{ active: form.scene_hint === 'web_analysis' }" @click="form.scene_hint = 'web_analysis'">Web 分析</button>
            <button type="button" :disabled="submitting" :class="{ active: form.scene_hint === 'incident_response' }" @click="form.scene_hint = 'incident_response'">日志分析</button>
            <button type="button" :disabled="submitting" :class="{ active: form.scene_hint === 'source_audit' }" @click="form.scene_hint = 'source_audit'">源码审计</button>
          </div>
        </div>
        <div>
          <span>安全策略</span>
          <div class="segmented">
            <button type="button" :disabled="submitting" :class="{ active: safetyMode === 'conservative' }" @click="safetyMode = 'conservative'">保守</button>
            <button type="button" :disabled="submitting" :class="{ active: safetyMode === 'standard' }" @click="safetyMode = 'standard'">标准</button>
            <button type="button" :disabled="submitting" :class="{ active: safetyMode === 'expert' }" @click="safetyMode = 'expert'">专家</button>
          </div>
        </div>
      </section>

      <section v-if="showAdvanced" class="advanced-panel">
        <label class="field">
          <span>授权 URL（可选）</span>
          <input v-model="form.target_url" :disabled="submitting" type="url" placeholder="https://example.com">
        </label>
        <label class="field">
          <span>模型路由</span>
          <select v-model="form.route_mode" :disabled="submitting">
            <option value="auto">自动协同</option>
            <option value="manual">手动指定</option>
          </select>
        </label>
        <label v-if="form.route_mode === 'manual'" class="field">
          <span>指定模型</span>
          <select v-model="form.preferred_model" :disabled="submitting" required>
            <option disabled value="">请选择</option>
            <option value="deepseek">DeepSeek</option>
            <option value="glm">GLM</option>
          </select>
        </label>
        <label class="field full">
          <span>授权范围补充（可选）</span>
          <textarea
            v-model="form.authorization_scope"
            :disabled="submitting"
            placeholder="不填则由系统根据任务描述、URL、附件和安全策略自动生成保守授权范围。"
          />
        </label>
      </section>

      <section class="authorization-preview">
        <strong>将提交的授权边界</strong>
        <p>{{ form.authorization_scope.trim() || generatedAuthorization }}</p>
      </section>

      <p v-if="error" class="error-message" role="alert">{{ error }}</p>
      <div v-if="submitting" class="plan-generating-status" role="status" aria-live="polite">
        <span class="loading-ring" aria-hidden="true"></span>
        <div>
          <strong>正在生成执行计划</strong>
          <p>系统正在创建任务、调用 AI 理解目标，并编排后续工具链，请稍等。</p>
        </div>
      </div>

      <div class="form-actions">
        <span>创建后会先生成 AI 解析结果和执行计划，再由系统自动调用白名单工具。</span>
        <button class="primary-button" :class="{ 'is-generating': submitting }" :disabled="submitting || !canSubmit" type="submit">
          <span v-if="submitting" class="button-spinner" aria-hidden="true"></span>
          {{ submitting ? '正在生成计划...' : '生成执行计划' }}
        </button>
      </div>
    </form>
  </section>
</template>
