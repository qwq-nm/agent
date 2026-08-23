<script setup lang="ts">
import { nextTick, onMounted, ref, watch } from 'vue'
import type { Evidence } from '../types'
import { confidenceLabel, evidenceContentLabel, evidenceTypeLabel } from '../labels'

const props = defineProps<{
  evidences: Evidence[]
  autoFocus?: boolean
}>()

const evidenceList = ref<HTMLElement | null>(null)

async function focusLatestEvidence() {
  if (!props.autoFocus || !evidenceList.value) return
  await nextTick()
  evidenceList.value.lastElementChild?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
}

onMounted(focusLatestEvidence)
watch(
  () => props.evidences.length,
  () => void focusLatestEvidence(),
)
</script>

<template>
  <div ref="evidenceList" class="evidence-list">
    <p v-if="!evidences.length" class="empty-state compact">暂时没有证据记录。</p>
    <article v-for="item in evidences" :key="item.id" class="evidence-card">
      <header>
        <span>{{ evidenceTypeLabel(item.evidence_type) }}</span>
        <b>置信度 {{ item.confidence.toFixed(2) }} · {{ confidenceLabel(item.confidence) }}</b>
      </header>
      <small class="raw-label">{{ item.evidence_type }}</small>
      <code>{{ item.source }}</code>
      <p>{{ evidenceContentLabel(item) }}</p>
      <code v-if="item.evidence_hash" class="evidence-hash" title="Evidence SHA-256">SHA-256: {{ item.evidence_hash }}</code>
    </article>
  </div>
</template>
