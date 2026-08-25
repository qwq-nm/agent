<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useAuthStore } from '../stores/auth'
import { useTasksStore } from '../stores/tasks'

const store = useTasksStore()
const auth = useAuthStore()
const statusFilter = ref('')
const sceneFilter = ref('')
onMounted(store.load)

const title = computed(() => auth.user?.role === 'admin' ? '全部任务' : '我的任务')
const filteredTasks = computed(() => store.tasks.filter((task) =>
  (!statusFilter.value || task.status === statusFilter.value)
  && (!sceneFilter.value || (task.scene || task.scene_hint) === sceneFilter.value),
))
const statuses = computed(() => [...new Set(store.tasks.map((task) => task.status))])
const scenes = computed(() => [...new Set(store.tasks.map((task) => task.scene || task.scene_hint).filter((scene): scene is string => Boolean(scene)))])
const sceneNames: Record<string, string> = {
  ctf_web: 'CTF Web', incident_response: '日志响应', source_audit: '源码审计', web_analysis: 'Web 分析', vulnerability_hunting: '漏洞挖掘', reverse_analysis: '逆向分析',
}
</script>

<template>
  <section class="page">
    <header class="page-header">
      <div><p class="eyebrow">MISSION CONTROL</p><h1>{{ title }}</h1><p>跟踪每个安全任务的状态、场景与模型路由。</p></div>
      <RouterLink class="primary-button link-button" to="/tasks/new">创建任务</RouterLink>
    </header>
    <div class="filters" aria-label="任务筛选">
      <label>状态 <select v-model="statusFilter"><option value="">全部</option><option v-for="status in statuses" :key="status" :value="status">{{ status }}</option></select></label>
      <label>场景 <select v-model="sceneFilter"><option value="">全部</option><option v-for="scene in scenes" :key="scene" :value="scene">{{ sceneNames[scene] || scene }}</option></select></label>
    </div>
    <div class="panel table-panel">
      <p v-if="store.loading" class="empty-state">正在加载任务…</p>
      <p v-else-if="store.error" class="error-message">{{ store.error }}</p>
      <p v-else-if="!filteredTasks.length" class="empty-state">没有符合筛选条件的任务。</p>
      <table v-else>
        <thead><tr><th>任务</th><th>场景</th><th>路由</th><th>状态</th><th></th></tr></thead>
        <tbody>
          <tr v-for="task in filteredTasks" :key="task.id">
            <td><strong>{{ task.goal }}</strong><small>{{ task.id.slice(0, 8) }}</small></td>
            <td>{{ sceneNames[task.scene || task.scene_hint || ''] || '待识别' }}</td>
            <td>{{ task.route_mode === 'auto' ? '自动协同' : task.preferred_model }}</td>
            <td><span class="status-badge" :data-status="task.status">{{ task.status }}</span><em v-if="task.is_demo">演示</em></td>
            <td><RouterLink :to="`/tasks/${task.id}`">查看详情 →</RouterLink></td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
