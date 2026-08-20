<script setup lang="ts">
import type { ModelCall } from '../types'
defineProps<{ calls: ModelCall[] }>()
</script>

<template>
  <div class="route-list">
    <article v-for="call in calls" :key="call.id" class="route-card">
      <header><strong>{{ call.provider.toUpperCase() }}</strong><span v-if="call.is_demo">演示</span></header>
      <p>{{ call.stage }} / {{ call.model }}</p>
      <small>{{ call.route_reason }} / {{ call.latency_ms }} ms</small>
      <dl class="model-metrics">
        <div><dt>Tokens</dt><dd>{{ call.prompt_tokens ?? call.input_tokens ?? 0 }} / {{ call.completion_tokens ?? call.output_tokens ?? 0 }} tokens</dd></div>
        <div><dt>Retries</dt><dd>{{ call.retry_count ?? 0 }}</dd></div>
        <div><dt>Request ID</dt><dd>{{ call.request_id || '—' }}</dd></div>
        <div v-if="call.error_code"><dt>Error code</dt><dd>{{ call.error_code }}</dd></div>
      </dl>
    </article>
    <p v-if="!calls.length" class="empty-state compact">暂无模型调用记录。</p>
  </div>
</template>
