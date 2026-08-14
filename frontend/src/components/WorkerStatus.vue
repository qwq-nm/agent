<script setup lang="ts">
defineProps<{
  queuePosition?: number | null
  attempt?: number | null
  heartbeatAt?: string | null
  stage?: string | null
}>()
</script>

<template>
  <dl class="worker-status" aria-label="Worker status">
    <div><dt>队列位置</dt><dd>{{ queuePosition ?? '—' }}</dd></div>
    <div><dt>尝试次数</dt><dd>{{ attempt ?? '—' }}</dd></div>
    <div><dt>Worker 心跳</dt><dd>{{ heartbeatAt ? new Date(heartbeatAt).toLocaleString() : '—' }}</dd></div>
    <div><dt>当前阶段</dt><dd>{{ stage || '—' }}</dd></div>
  </dl>
</template>
