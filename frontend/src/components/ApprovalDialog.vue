<script setup lang="ts">
import { ref, watch } from 'vue'
import type { PendingApproval } from '../types'

const props = defineProps<{ open: boolean; busy?: boolean; approval?: PendingApproval | null }>()
const emit = defineEmits<{ close: []; decide: [approved: boolean, reason: string] }>()
const reason = ref('')
watch(() => props.open, (value) => { if (value) reason.value = '' })
</script>

<template>
  <div v-if="open && approval" class="dialog-backdrop" role="presentation" @click.self="emit('close')">
    <section class="approval-dialog" role="dialog" aria-modal="true" aria-labelledby="approval-title">
      <p class="eyebrow">HUMAN IN THE LOOP</p><h2 id="approval-title">确认中风险工具调用</h2><p>请核对目标、工具与参数。批准只对当前工具生效。</p>
      <dl><div><dt>工具</dt><dd><code>{{ approval.tool_name }}</code></dd></div><div><dt>风险</dt><dd>{{ approval.risk_level }}</dd></div><div><dt>参数/目标</dt><dd><code>{{ approval.params_summary }}</code></dd></div></dl>
      <label class="field"><span>审批理由</span><textarea v-model="reason" placeholder="说明批准或拒绝的依据" /></label>
      <div class="dialog-actions"><button class="ghost-button danger" type="button" :disabled="busy" @click="emit('decide', false, reason || '人工拒绝')">拒绝</button><button class="primary-button" type="button" :disabled="busy" @click="emit('decide', true, reason || '已核对授权范围')">批准并继续</button></div>
    </section>
  </div>
</template>
