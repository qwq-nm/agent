<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../api/client'
import type { ModelStatus, ProviderCheck, ReadinessStatus, ToolStatus, WorkerSummary } from '../types'

const models = ref<ModelStatus[]>([])
const tools = ref<ToolStatus[]>([])
const workers = ref<WorkerSummary>({ online: 0, active: 0, capacity: 3, queued: 0 })
const readiness = ref<ReadinessStatus>({ status: 'checking', checks: {} })
const checks = ref<Record<string, ProviderCheck>>({})
const checking = ref('')
const error = ref('')

async function load() {
  error.value = ''
  const results = await Promise.allSettled([
    api.modelStatus(),
    api.toolStatus(),
    api.workers(),
    api.readiness(),
  ])
  if (results[0].status === 'fulfilled') models.value = results[0].value
  if (results[1].status === 'fulfilled') tools.value = results[1].value
  if (results[2].status === 'fulfilled') workers.value = results[2].value
  if (results[3].status === 'fulfilled') readiness.value = results[3].value
  if (results.some((result) => result.status === 'rejected')) error.value = '部分运行状态暂时不可用'
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
    <header class="page-header"><div><p class="eyebrow">RUNTIME INVENTORY</p><h1>运行状态</h1><p>展示队列、Worker 和 Provider 的安全摘要；密钥只在后端读取，页面永远不接收 Secret。</p></div></header>
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
    <section class="panel readiness-panel"><h2>依赖检查</h2><div v-for="(value, key) in readiness.checks" :key="key"><span>{{ key }}</span><b :data-status="value">{{ value }}</b></div></section>
  </section>
</template>
