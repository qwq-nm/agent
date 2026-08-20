<script setup lang="ts">
import { Close, Delete, DocumentChecked } from '@element-plus/icons-vue'
import { nextTick, onMounted, ref } from 'vue'
import { api } from '../api/client'
import type { ModelStatus, ProviderCheck, ProviderCredential, ProviderName, ProviderRoute, ProviderRouteOption, ReadinessStatus, ToolStatus, WorkerSummary } from '../types'

const models = ref<ModelStatus[]>([])
const tools = ref<ToolStatus[]>([])
const workers = ref<WorkerSummary>({ online: 0, active: 0, capacity: 3, queued: 0 })
const readiness = ref<ReadinessStatus>({ status: 'checking', checks: {} })
const checks = ref<Record<string, ProviderCheck>>({})
const checking = ref('')
const credentials = ref<ProviderCredential[]>([])
const deepseekRoute = ref<ProviderRoute>()
const deepseekRouteOptions = ref<ProviderRouteOption[]>([])
const routeBusy = ref('')
const providerKeys = ref<Record<ProviderName, string>>({ deepseek: '', glm: '' })
const credentialBusy = ref<Record<ProviderName, boolean>>({ deepseek: false, glm: false })
const pendingClearProvider = ref<ProviderName | null>(null)
const clearDialog = ref<HTMLFormElement | null>(null)
const clearDialogCloseButton = ref<HTMLButtonElement | null>(null)
const clearDialogInvoker = ref<HTMLButtonElement | null>(null)
const error = ref('')
const providers: ProviderName[] = ['deepseek', 'glm']

const providerLabels: Record<ProviderName, { title: string; subtitle: string; keyHint: string }> = {
  deepseek: {
    title: 'DeepSeek 路由密钥',
    subtitle: '根据下方运行配置，可接 OpenCode Go 或 DeepSeek 官方。',
    keyHint: '请输入当前路由后端对应的 API Key',
  },
  glm: {
    title: '智谱 GLM 密钥',
    subtitle: '用于中文任务解析和中文报告生成。',
    keyHint: '请输入 GLM API Key',
  },
}

function modelLabel(model: ModelStatus): string {
  return model.display_name || providerLabel(model.name).title
}

function providerLabel(provider: string): { title: string; subtitle: string; keyHint: string } {
  if (provider === 'deepseek' || provider === 'glm') return providerLabels[provider]
  if (provider === 'mock') {
    return {
      title: 'Mock 演示模型',
      subtitle: '离线演示，不需要 API Key。',
      keyHint: '无需填写',
    }
  }
  return { title: provider, subtitle: '', keyHint: '请输入 API Key' }
}

function deepseekStatus(): ModelStatus | undefined {
  return models.value.find((model) => model.name === 'deepseek')
}

function activeDeepseekRoute(option: string): boolean {
  return (deepseekRoute.value?.route || deepseekStatus()?.api_style) === option
}

function routeEnv(option: ProviderRouteOption): string {
  const lines = [
    `DEEPSEEK_BASE_URL=${option.base_url}`,
    `DEEPSEEK_MODEL=${option.model}`,
    `DEEPSEEK_API_STYLE=${option.api_style}`,
  ]
  if (option.reasoning_effort) lines.push(`DEEPSEEK_REASONING_EFFORT=${option.reasoning_effort}`)
  return lines.join('\n')
}

function routeDescription(option: ProviderRouteOption): string {
  if (option.route === 'opencode-go') {
    return '选择后，DeepSeek 路由密钥中应填写 OpenCode Go 的 API Key。'
  }
  return '选择后，DeepSeek 路由密钥中应填写 DeepSeek 官方平台 API Key。'
}

async function saveDeepseekRoute(route: string) {
  if (routeBusy.value || activeDeepseekRoute(route)) return
  routeBusy.value = route
  error.value = ''
  try {
    deepseekRoute.value = await api.saveDeepseekRoute(route)
    await load()
  } catch (value) {
    error.value = value instanceof Error ? value.message : 'DeepSeek 路由配置保存失败'
  } finally {
    routeBusy.value = ''
  }
}

async function load() {
  error.value = ''
  const results = await Promise.allSettled([
    api.modelStatus(),
    api.toolStatus(),
    api.workers(),
    api.readiness(),
    api.listProviderCredentials(),
    api.getDeepseekRoute(),
    api.listDeepseekRouteOptions(),
  ])
  if (results[0].status === 'fulfilled') models.value = results[0].value
  if (results[1].status === 'fulfilled') tools.value = results[1].value
  if (results[2].status === 'fulfilled') workers.value = results[2].value
  if (results[3].status === 'fulfilled') readiness.value = results[3].value
  if (results[4].status === 'fulfilled') credentials.value = results[4].value
  if (results[5].status === 'fulfilled') deepseekRoute.value = results[5].value
  if (results[6].status === 'fulfilled') deepseekRouteOptions.value = results[6].value
  if (results.some((result) => result.status === 'rejected')) error.value = '部分运行状态暂时不可用'
}

function credential(provider: ProviderName): ProviderCredential {
  return credentials.value.find((item) => item.provider === provider) ?? { provider, configured: false }
}

async function refreshCredentials() {
  credentials.value = await api.listProviderCredentials()
}

async function saveProviderCredential(provider: ProviderName) {
  const apiKey = providerKeys.value[provider]
  if (!apiKey || credentialBusy.value[provider]) return
  credentialBusy.value[provider] = true
  error.value = ''
  try {
    await api.saveProviderCredential(provider, apiKey)
    await refreshCredentials()
  } catch (value) {
    error.value = value instanceof Error ? value.message : 'Provider 密钥保存失败'
  } finally {
    providerKeys.value[provider] = ''
    credentialBusy.value[provider] = false
  }
}

async function requestClearProviderCredential(provider: ProviderName, event: MouseEvent) {
  if (credentialBusy.value[provider] || !credential(provider).configured) return
  clearDialogInvoker.value = event.currentTarget instanceof HTMLButtonElement ? event.currentTarget : null
  pendingClearProvider.value = provider
  await nextTick()
  clearDialogCloseButton.value?.focus()
}

async function closeClearProviderDialog() {
  const invoker = clearDialogInvoker.value
  pendingClearProvider.value = null
  await nextTick()
  invoker?.focus()
  clearDialogInvoker.value = null
}

function trapClearDialogFocus(event: KeyboardEvent) {
  const controls = Array.from(clearDialog.value?.querySelectorAll<HTMLElement>('button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])') ?? [])
  if (!controls.length) return
  const first = controls[0]
  const last = controls[controls.length - 1]
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}

async function cancelClearProviderCredential() {
  await closeClearProviderDialog()
}

async function clearProviderCredential() {
  const provider = pendingClearProvider.value
  if (!provider || credentialBusy.value[provider]) return
  credentialBusy.value[provider] = true
  error.value = ''
  let cleared = false
  try {
    await api.clearProviderCredential(provider)
    await refreshCredentials()
    cleared = true
  } catch (value) {
    error.value = value instanceof Error ? value.message : 'Provider 密钥清除失败'
  } finally {
    providerKeys.value[provider] = ''
    credentialBusy.value[provider] = false
    if (cleared) await closeClearProviderDialog()
  }
}

async function providerCheck(name: string) {
  checking.value = name
  error.value = ''
  try { checks.value[name] = await api.providerCheck(name) }
  catch (value) { error.value = value instanceof Error ? value.message : 'Provider 检查失败' }
  finally { checking.value = '' }
}

onMounted(load)
</script>

<template>
  <section class="page">
    <header class="page-header"><div><p class="eyebrow">RUNTIME INVENTORY</p><h1>运行状态</h1><p>展示队列、Worker 和 Provider 的安全摘要；已保存密钥不会显示在页面中。</p></div></header>
    <p v-if="error" class="error-message" role="alert">{{ error }}</p>
    <div class="metric-grid runtime-metrics">
      <article class="metric-card"><span>Worker 在线</span><strong>{{ workers.online }}</strong><small>当前可见 Worker</small></article>
      <article class="metric-card"><span>活动任务</span><strong>{{ workers.active }} / {{ workers.capacity }}</strong><small>并发上限</small></article>
      <article class="metric-card"><span>队列等待</span><strong>{{ workers.queued }}</strong><small>待领取任务</small></article>
      <article class="metric-card"><span>服务就绪</span><strong>{{ readiness.status }}</strong><small>数据库 / Redis / 配置</small></article>
    </div>
    <div class="system-grid">
      <section class="panel system-panel">
        <h2>Provider 状态</h2>
        <article v-for="model in models" :key="model.name">
          <div><strong>{{ modelLabel(model) }}</strong><small>{{ model.model || model.mode }}<template v-if="model.api_style"> · {{ model.api_style }}</template></small></div>
          <span>{{ model.status || model.mode }}</span>
          <b>{{ model.configured ? '已配置' : '未配置' }}</b>
          <button class="ghost-button" :data-action="`provider-check-${model.name}`" type="button" :disabled="checking === model.name" @click="providerCheck(model.name)">{{ checking === model.name ? '检查中…' : '连通性检查' }}</button>
          <p v-if="checks[model.name]" class="check-result">{{ checks[model.name].status }} · {{ checks[model.name].request_id || '无 request ID' }} · {{ checks[model.name].input_tokens ?? '—' }} / {{ checks[model.name].output_tokens ?? '—' }} tokens · {{ checks[model.name].latency_ms ?? '—' }} ms<span v-if="checks[model.name].error_code"> · {{ checks[model.name].error_code }}</span></p>
        </article>
      </section>
      <section class="panel system-panel"><h2>白名单工具</h2><article v-for="tool in tools" :key="tool.name"><strong>{{ tool.name }}</strong><span>{{ tool.scene }}</span><b>{{ tool.risk_level }}</b></article></section>
    </div>
    <section class="panel credentials-panel">
      <h2>Provider 密钥</h2>
      <div class="route-options" aria-label="DeepSeek 路由配置选项">
        <article v-for="option in deepseekRouteOptions" :key="option.route" :class="{ active: activeDeepseekRoute(option.route) }">
          <div>
            <strong>{{ option.display_name }}</strong>
            <span v-if="activeDeepseekRoute(option.route)">当前配置</span>
          </div>
          <p>{{ routeDescription(option) }}</p>
          <pre>{{ routeEnv(option) }}</pre>
          <button class="ghost-button" type="button" :disabled="routeBusy === option.route || activeDeepseekRoute(option.route)" @click="saveDeepseekRoute(option.route)">
            {{ activeDeepseekRoute(option.route) ? '已选择' : routeBusy === option.route ? '保存中…' : '选择此路由' }}
          </button>
        </article>
      </div>
      <div class="credential-grid">
        <article v-for="provider in providers" :key="provider" class="credential-row" :class="{ 'is-busy': credentialBusy[provider] }">
          <div class="credential-provider"><strong>{{ providerLabel(provider).title }}</strong><small>{{ providerLabel(provider).subtitle }}</small><small>{{ credential(provider).configured ? '已配置' : '未配置' }}</small></div>
          <div class="credential-status"><span v-if="credential(provider).key_hint">{{ credential(provider).key_hint }}</span><span v-else>未设置</span><small v-if="credential(provider).updated_at">{{ credential(provider).updated_at }}</small></div>
          <label class="credential-key"><span class="sr-only">{{ providerLabel(provider).title }}</span><input v-model="providerKeys[provider]" :name="`provider-key-${provider}`" type="password" autocomplete="new-password" :placeholder="providerLabel(provider).keyHint" :disabled="credentialBusy[provider]"></label>
          <div class="credential-actions">
            <button class="icon-button" :data-action="`save-provider-${provider}`" type="button" :disabled="credentialBusy[provider] || !providerKeys[provider]" :aria-label="`${providerLabel(provider).title} 保存密钥`" :title="`${providerLabel(provider).title} 保存密钥`" @click="saveProviderCredential(provider)"><DocumentChecked /></button>
            <button class="icon-button danger" :data-action="`clear-provider-${provider}`" type="button" :disabled="credentialBusy[provider]" :aria-disabled="!credential(provider).configured || undefined" :aria-label="`${providerLabel(provider).title} 清除密钥`" :title="`${providerLabel(provider).title} 清除密钥`" @click="requestClearProviderCredential(provider, $event)"><Delete /></button>
          </div>
        </article>
      </div>
    </section>
    <section class="panel readiness-panel"><h2>依赖检查</h2><div v-for="(value, key) in readiness.checks" :key="key"><span>{{ key }}</span><b :data-status="value">{{ value }}</b></div></section>
    <div v-if="pendingClearProvider" class="dialog-backdrop" role="presentation">
      <form ref="clearDialog" class="admin-dialog panel" data-form="clear-provider" role="dialog" aria-modal="true" aria-labelledby="clear-provider-title" @keydown.esc.prevent="cancelClearProviderCredential" @keydown.tab="trapClearDialogFocus" @submit.prevent="clearProviderCredential">
        <header class="panel-title"><div><p class="eyebrow">DISRUPTIVE CHANGE</p><h2 id="clear-provider-title">清除 Provider 密钥</h2></div><button ref="clearDialogCloseButton" type="button" class="icon-button" aria-label="关闭" title="关闭" @click="cancelClearProviderCredential"><Close /></button></header>
        <div class="admin-dialog-body"><p class="dialog-context">将清除 {{ pendingClearProvider }} 的已保存密钥。</p></div>
        <footer class="dialog-actions"><button type="button" class="ghost-button" @click="cancelClearProviderCredential">取消</button><button class="ghost-button danger" data-action="confirm-clear-provider" type="submit" :disabled="credentialBusy[pendingClearProvider]">确认清除</button></footer>
      </form>
    </div>
  </section>
</template>
