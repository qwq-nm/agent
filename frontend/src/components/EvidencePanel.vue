<script setup lang="ts">
import type { Evidence } from '../types'
defineProps<{ evidences: Evidence[] }>()
</script>

<template>
  <div class="evidence-list">
    <p v-if="!evidences.length" class="empty-state compact">暂时没有证据记录。</p>
    <article v-for="item in evidences" :key="item.id" class="evidence-card">
      <header><span>{{ item.evidence_type }}</span><b>{{ item.confidence.toFixed(2) }}</b></header>
      <code>{{ item.source }}</code>
      <p>{{ item.content }}</p>
      <code v-if="item.evidence_hash" class="evidence-hash" title="Evidence SHA-256">SHA-256: {{ item.evidence_hash }}</code>
    </article>
  </div>
</template>
