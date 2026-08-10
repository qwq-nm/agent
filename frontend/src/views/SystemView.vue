<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../api/client'
import type { ModelStatus, ToolStatus } from '../types'
const models = ref<ModelStatus[]>([])
const tools = ref<ToolStatus[]>([])
onMounted(async () => { [models.value, tools.value] = await Promise.all([api.modelStatus(), api.toolStatus()]) })
</script>

<template>
  <section class="page"><header class="page-header"><div><p class="eyebrow">RUNTIME INVENTORY</p><h1>模型与工具</h1><p>仅显示后端实际配置的模型提供方和已注册白名单工具。</p></div></header>
    <div class="system-grid"><section class="panel system-panel"><h2>模型提供方</h2><article v-for="model in models" :key="model.name"><strong>{{ model.name }}</strong><span>{{ model.mode }}</span><b>{{ model.configured ? '已配置' : '未配置' }}</b></article></section>
    <section class="panel system-panel"><h2>白名单工具</h2><article v-for="tool in tools" :key="tool.name"><strong>{{ tool.name }}</strong><span>{{ tool.scene }}</span><b>{{ tool.risk_level }}</b></article></section></div>
  </section>
</template>
