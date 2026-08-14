<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../api/client'
import { useAuthStore } from '../stores/auth'
import { useTasksStore } from '../stores/tasks'

const store = useTasksStore()
const auth = useAuthStore()
const service = ref('checking')
const completed = computed(() => store.tasks.filter((task) => task.status === 'completed').length)
const waiting = computed(() => store.tasks.filter((task) => task.status === 'waiting_human').length)
onMounted(async () => { await store.load(); try { service.value = (await api.health()).status } catch { service.value = 'unavailable' } })
</script>

<template>
  <section class="page">
    <header class="page-header hero-header"><div><p class="eyebrow">SECAGENT-X / AUTONOMOUS DEFENSE</p><h1>{{ auth.user?.role === 'admin' ? '全部任务概览' : '我的任务概览' }}</h1><p>模型负责理解与推理，所有结论都可回溯至证据链。</p></div><RouterLink class="primary-button link-button" to="/tasks/new">启动新任务</RouterLink></header>
    <div class="metric-grid"><article class="metric-card"><span>服务状态</span><strong>{{ service }}</strong><small>来自 /api/health</small></article><article class="metric-card"><span>活动任务</span><strong>{{ store.activeCount }}</strong><small>当前非终态任务</small></article><article class="metric-card"><span>等待审批</span><strong>{{ waiting }}</strong><small>需要人工确认</small></article><article class="metric-card"><span>已完成</span><strong>{{ completed }}</strong><small>来自任务数据</small></article></div>
  </section>
</template>
