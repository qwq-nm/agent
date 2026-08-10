<script setup lang="ts">
import type { ModelCall } from '../types'
defineProps<{ calls: ModelCall[] }>()
</script>

<template>
  <div class="route-list">
    <article v-for="call in calls" :key="call.id" class="route-card">
      <header><strong>{{ call.provider.toUpperCase() }}</strong><span v-if="call.is_demo">演示</span></header>
      <p>{{ call.stage }} · {{ call.model }}</p>
      <small>{{ call.route_reason }} / {{ call.latency_ms }} ms</small>
    </article>
    <p v-if="!calls.length" class="empty-state compact">暂无模型调用记录。</p>
  </div>
</template>
