<script setup lang="ts">
import { computed } from 'vue'
import { statusLabel, toolNameLabel } from '../labels'
import type { RuntimeMemory } from '../types'

const props = defineProps<{
  memory?: RuntimeMemory
}>()

const memory = computed(
  () =>
    props.memory || {
      visited_urls: [],
      queued_urls: [],
      discovered_links: [],
      forms: [],
      parameters: [],
      cookies: [],
      js_files: [],
      api_endpoints: [],
      robots_paths: [],
      sensitive_paths: [],
      candidate_flags: [],
      interesting_findings: [],
      failed_tools: [],
      tool_result_summary: [],
      last_new_evidence_at: null,
    },
)

const statItems = computed(() => [
  { label: '已访问 URL', value: memory.value.visited_urls.length },
  { label: '待分析 URL', value: memory.value.queued_urls.length },
  { label: '页面链接', value: memory.value.discovered_links.length },
  { label: '表单', value: memory.value.forms.length },
  { label: '参数', value: memory.value.parameters.length },
  { label: '候选 Flag', value: memory.value.candidate_flags.length },
  { label: '失败工具', value: memory.value.failed_tools.length },
])

const importantPaths = computed(() => [
  ...memory.value.sensitive_paths,
  ...memory.value.robots_paths,
  ...memory.value.api_endpoints,
].slice(0, 40))

function formTitle(form: Record<string, unknown>) {
  const method = String(form.method || 'GET').toUpperCase()
  const action = String(form.action || '当前页面')
  return `${method} ${action}`
}

function formInputs(form: Record<string, unknown>) {
  const inputs = Array.isArray(form.inputs) ? form.inputs : []
  return inputs
    .map((item) => {
      if (!item || typeof item !== 'object') return ''
      const value = item as Record<string, unknown>
      return String(value.name || value.type || '').trim()
    })
    .filter(Boolean)
    .join('、')
}
</script>

<template>
  <section class="panel runtime-memory-panel">
    <header class="panel-title">
      <div>
        <p class="eyebrow">RUNTIME MEMORY</p>
        <h2>任务运行记忆</h2>
      </div>
      <span>从证据账本自动归纳</span>
    </header>

    <div class="memory-stats">
      <article v-for="item in statItems" :key="item.label">
        <span>{{ item.label }}</span>
        <strong>{{ item.value }}</strong>
      </article>
    </div>

    <div class="memory-grid">
      <article>
        <h3>下一步可追踪线索</h3>
        <ul v-if="importantPaths.length">
          <li v-for="item in importantPaths" :key="item">{{ item }}</li>
        </ul>
        <p v-else class="empty-state compact">暂未发现敏感路径、robots 线索或 API 入口。</p>
      </article>

      <article>
        <h3>待分析 URL</h3>
        <ul v-if="memory.queued_urls.length">
          <li v-for="item in memory.queued_urls.slice(0, 40)" :key="item">{{ item }}</li>
        </ul>
        <p v-else class="empty-state compact">当前没有新的待分析 URL。</p>
      </article>

      <article>
        <h3>表单与参数</h3>
        <div v-if="memory.forms.length" class="memory-form-list">
          <div v-for="form in memory.forms.slice(0, 20)" :key="formTitle(form)">
            <strong>{{ formTitle(form) }}</strong>
            <span>{{ formInputs(form) || '未提取到字段名' }}</span>
          </div>
        </div>
        <p v-else-if="memory.parameters.length">{{ memory.parameters.join('、') }}</p>
        <p v-else class="empty-state compact">暂未发现公开表单或参数。</p>
      </article>

      <article>
        <h3>候选 Flag 与关键发现</h3>
        <ul v-if="memory.candidate_flags.length || memory.interesting_findings.length">
          <li v-for="item in memory.candidate_flags" :key="`flag-${item}`">候选 Flag：{{ item }}</li>
          <li v-for="item in memory.interesting_findings.slice(0, 20)" :key="item">{{ item }}</li>
        </ul>
        <p v-else class="empty-state compact">暂未发现候选 Flag 或显著关键字。</p>
      </article>

      <article>
        <h3>失败工具记录</h3>
        <div v-if="memory.failed_tools.length" class="memory-failure-list">
          <div v-for="item in memory.failed_tools.slice(0, 20)" :key="`${item.tool_name}-${item.step_name}-${item.error}`">
            <strong>{{ item.step_name || toolNameLabel(item.tool_name) }}</strong>
            <span>{{ toolNameLabel(item.tool_name) }} · {{ statusLabel(item.status) }}</span>
            <p>{{ item.error || item.summary || '未记录具体错误' }}</p>
          </div>
        </div>
        <p v-else class="empty-state compact">当前没有失败工具记录。</p>
      </article>

      <article>
        <h3>已访问 URL</h3>
        <ul v-if="memory.visited_urls.length">
          <li v-for="item in memory.visited_urls.slice(0, 40)" :key="item">{{ item }}</li>
        </ul>
        <p v-else class="empty-state compact">尚未产生真实访问记录。</p>
      </article>
    </div>
  </section>
</template>
