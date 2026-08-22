<script setup lang="ts">
defineProps<{
  queuePosition?: number | null
  attempt?: number | null
  heartbeatAt?: string | null
  stage?: string | null
}>()
</script>

<template>
  <dl class="worker-status" aria-label="后台执行状态">
    <div>
      <dt>队列位置</dt>
      <dd>{{ queuePosition ?? '-' }}</dd>
    </div>
    <div>
      <dt>尝试次数</dt>
      <dd>{{ attempt ?? '-' }}</dd>
    </div>
    <div>
      <dt>后台执行更新时间</dt>
      <dd>{{ heartbeatAt ? new Date(heartbeatAt).toLocaleString() : '-' }}</dd>
    </div>
    <div>
      <dt>当前阶段</dt>
      <dd>{{ stage || '-' }}</dd>
    </div>
  </dl>
</template>
