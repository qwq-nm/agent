<script setup lang="ts">
import type { ModelCall } from '../types'
import { errorCodeLabel, providerName, routeReasonLabel, stageLabel } from '../labels'

defineProps<{ calls: ModelCall[] }>()
</script>

<template>
  <div class="route-list">
    <article v-for="call in calls" :key="call.id" class="route-card">
      <header><strong>{{ providerName(call.provider) }}</strong><span v-if="call.is_demo">演示</span></header>
      <p>{{ stageLabel(call.stage) }} / {{ call.model }}</p>
      <small>{{ routeReasonLabel(call.route_reason) }} / 耗时 {{ call.latency_ms }} ms</small>
      <dl class="model-metrics">
        <div><dt>Token 用量</dt><dd>{{ call.prompt_tokens ?? call.input_tokens ?? 0 }} / {{ call.completion_tokens ?? call.output_tokens ?? 0 }}</dd></div>
        <div><dt>重试次数</dt><dd>{{ call.retry_count ?? 0 }}</dd></div>
        <div><dt>请求编号</dt><dd>{{ call.request_id || '—' }}</dd></div>
        <div v-if="call.error_code"><dt>错误原因</dt><dd>{{ errorCodeLabel(call.error_code) }}</dd></div>
      </dl>
    </article>
    <p v-if="!calls.length" class="empty-state compact">暂无模型调用记录。</p>
  </div>
</template>
