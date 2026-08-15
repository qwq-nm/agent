<script setup lang="ts">
import { Close, Delete, DocumentChecked } from '@element-plus/icons-vue'
import { nextTick, onMounted, ref } from 'vue'
import { api } from '../api/client'
import type { ModelStatus, ProviderCheck, ProviderCredential, ProviderName, ReadinessStatus, ToolStatus, WorkerSummary } from '../types'

const models = ref<ModelStatus[]>([])
const tools = ref<ToolStatus[]>([])
const workers = ref<WorkerSummary>({ online: 0, active: 0, capacity: 3, queued: 0 })
const readiness = ref<ReadinessStatus>({ status: 'checking', checks: {} })
const checks = ref<Record<string, ProviderCheck>>({})
const checking = ref('')
const credentials = ref<ProviderCredential[]>([])
const providerKeys = ref<Record<ProviderName, string>>({ deepseek: '', glm: '' })
const credentialBusy = ref<Record<ProviderName, boolean>>({ deepseek: false, glm: false })
const pendingClearProvider = ref<ProviderName | null>(null)
const clearDialogCloseButton = ref<HTMLButtonElement | null>(null)
const error = ref('')
const providers: ProviderName[] = ['deepseek', 'glm']

async function load() {
  error.value = ''
  const results = await Promise.allSettled([
    api.modelStatus(),
    api.toolStatus(),
    api.workers(),
    api.readiness(),
    api.listProviderCredentials(),
  ])
  if (results[0].status === 'fulfilled') models.value = results[0].value
  if (results[1].status === 'fulfilled') tools.value = results[1].value
  if (results[2].status === 'fulfilled') workers.value = results[2].value
  if (results[3].status === 'fulfilled') readiness.value = results[3].value
  if (results[4].status === 'fulfilled') credentials.value = results[4].value
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

async function requestClearProviderCredential(provider: ProviderName) {
  if (credentialBusy.value[provider]) return
  pendingClearProvider.value = provider
  await nextTick()
  clearDialogCloseButton.value?.focus()
}

function cancelClearProviderCredential() {
  pendingClearProvider.value = null
}

async function clearProviderCredential() {
  const provider = pendingClearProvider.value
  if (!provider || credentialBusy.value[provider]) return
  credentialBusy.value[provider] = true
  error.value = ''
  try {
    await api.clearProviderCredential(provider)
    await refreshCredentials()
    pendingClearProvider.value = null
  } catch (value) {
    error.value = value instanceof Error ? value.message : 'Provider 密钥清除失败'
  } finally {
    providerKeys.value[provider] = ''
    credentialBusy.value[provider] = false
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
          <div><strong>{{ model.name }}</strong><small>{{ model.model || model.mode }}</small></div>
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
      <div class="credential-grid">
        <article v-for="provider in providers" :key="provider" class="credential-row" :class="{ 'is-busy': credentialBusy[provider] }">
          <div class="credential-provider"><strong>{{ provider }}</strong><small>{{ credential(provider).configured ? '已配置' : '未配置' }}</small></div>
          <div class="credential-status"><span v-if="credential(provider).key_hint">{{ credential(provider).key_hint }}</span><span v-else>未设置</span><small v-if="credential(provider).updated_at">{{ credential(provider).updated_at }}</small></div>
          <label class="credential-key"><span class="sr-only">{{ provider }} API Key</span><input v-model="providerKeys[provider]" :name="`provider-key-${provider}`" type="password" autocomplete="new-password" :disabled="credentialBusy[provider]"></label>
          <div class="credential-actions">
            <button class="icon-button" :data-action="`save-provider-${provider}`" type="button" :disabled="credentialBusy[provider] || !providerKeys[provider]" :aria-label="`${provider} 保存密钥`" :title="`${provider} 保存密钥`" @click="saveProviderCredential(provider)"><DocumentChecked /></button>
            <button class="icon-button danger" :data-action="`clear-provider-${provider}`" type="button" :disabled="credentialBusy[provider] || !credential(provider).configured" :aria-label="`${provider} 清除密钥`" :title="`${provider} 清除密钥`" @click="requestClearProviderCredential(provider)"><Delete /></button>
          </div>
        </article>
      </div>
    </section>
    <section class="panel readiness-panel"><h2>依赖检查</h2><div v-for="(value, key) in readiness.checks" :key="key"><span>{{ key }}</span><b :data-status="value">{{ value }}</b></div></section>
    <div v-if="pendingClearProvider" class="dialog-backdrop" role="presentation">
      <form class="admin-dialog panel" data-form="clear-provider" role="dialog" aria-modal="true" aria-labelledby="clear-provider-title" @keydown.esc="cancelClearProviderCredential" @submit.prevent="clearProviderCredential">
        <header class="panel-title"><div><p class="eyebrow">DISRUPTIVE CHANGE</p><h2 id="clear-provider-title">清除 Provider 密钥</h2></div><button ref="clearDialogCloseButton" type="button" class="icon-button" aria-label="关闭" title="关闭" @click="cancelClearProviderCredential"><Close /></button></header>
        <div class="admin-dialog-body"><p class="dialog-context">将清除 {{ pendingClearProvider }} 的已保存密钥。</p></div>
        <footer class="dialog-actions"><button type="button" class="ghost-button" @click="cancelClearProviderCredential">取消</button><button class="ghost-button danger" data-action="confirm-clear-provider" type="submit" :disabled="credentialBusy[pendingClearProvider]">确认清除</button></footer>
      </form>
    </div>
  </section>
</template>
