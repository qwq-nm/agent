<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../api/client'
import type { AuditEvent } from '../types'

const events = ref<AuditEvent[]>([])
const actor = ref('')
const action = ref('')
const outcome = ref('')
const createdAfter = ref('')
const createdBefore = ref('')
const loading = ref(false)
const error = ref('')
const before = ref<number | undefined>()

function safeDetail(event: AuditEvent): string {
  const details = event.details || {}
  const allowed = ['reason', 'changed_fields', 'provider', 'model', 'request_id', 'error_code']
  return allowed
    .filter((key) => details[key] !== undefined)
    .map((key) => `${key}: ${String(details[key])}`)
    .join(' · ')
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    events.value = await api.auditEvents({ limit: 100, before: before.value, actor: actor.value, action: action.value, outcome: outcome.value, created_after: createdAfter.value, created_before: createdBefore.value })
  } catch (value) { error.value = value instanceof Error ? value.message : '审计记录加载失败' }
  finally { loading.value = false }
}

function nextPage() {
  const last = events.value[events.value.length - 1]
  if (last) { before.value = last.id; void load() }
}

function resetPage() { before.value = undefined; void load() }

onMounted(load)
</script>

<template>
  <section class="page">
    <header class="page-header"><div><p class="eyebrow">AUDIT LEDGER</p><h1>操作审计</h1><p>仅管理员可见的追加式记录；详情经过字段白名单过滤，不展示密码、令牌或 Provider Secret。</p></div></header>
    <form class="filters audit-filters" data-form="audit-filters" @submit.prevent="resetPage"><label>操作者<input v-model="actor" name="actor" placeholder="用户名或 ID"></label><label>动作<input v-model="action" name="action" placeholder="例如 task.run"></label><label>结果<select v-model="outcome" name="outcome"><option value="">全部</option><option value="success">成功</option><option value="failure">失败</option><option value="denied">拒绝</option></select></label><label>起始时间<input v-model="createdAfter" name="created-after" type="datetime-local"></label><label>结束时间<input v-model="createdBefore" name="created-before" type="datetime-local"></label><button class="ghost-button" type="submit">筛选</button></form>
    <p v-if="error" class="error-message" role="alert">{{ error }}</p>
    <div class="panel table-panel admin-table">
      <p v-if="loading" class="empty-state">正在加载审计记录…</p>
      <p v-else-if="!events.length" class="empty-state">暂无符合条件的记录。</p>
      <table v-else>
        <thead><tr><th>ID / 时间</th><th>操作者</th><th>动作</th><th>资源</th><th>结果</th><th>安全详情</th></tr></thead>
        <tbody><tr v-for="event in events" :key="event.id"><td><strong>#{{ event.id }}</strong><small>{{ new Date(event.created_at).toLocaleString() }}</small></td><td>{{ event.actor_username || event.actor_id || '系统' }}</td><td><code>{{ event.action }}</code></td><td>{{ event.resource_type }} / {{ event.resource_id || '—' }}</td><td><span class="status-badge" :data-status="event.outcome">{{ event.outcome }}</span></td><td class="audit-detail">{{ safeDetail(event) || '—' }}</td></tr></tbody>
      </table>
    </div>
    <footer class="pagination"><button class="ghost-button" type="button" :disabled="!before" @click="resetPage">回到最新</button><button class="ghost-button" type="button" :disabled="events.length < 100" @click="nextPage">更早记录</button></footer>
  </section>
</template>
