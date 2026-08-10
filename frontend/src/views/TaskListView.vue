<script setup lang="ts">
import { onMounted } from 'vue'
import { useTasksStore } from '../stores/tasks'

const store = useTasksStore()
onMounted(store.load)

const sceneNames: Record<string, string> = {
  incident_response: '日志响应',
  source_audit: '源码审计',
  web_analysis: 'Web 分析',
}
</script>

<template>
  <section class="page">
    <header class="page-header">
      <div>
        <p class="eyebrow">MISSION CONTROL</p>
        <h1>任务中心</h1>
        <p>追踪每个安全任务的状态、场景与模型路由。</p>
      </div>
      <RouterLink class="primary-button link-button" to="/tasks/new">创建任务</RouterLink>
    </header>
    <div class="panel table-panel">
      <p v-if="store.loading" class="empty-state">正在加载任务…</p>
      <p v-else-if="store.error" class="error-message">{{ store.error }}</p>
      <p v-else-if="!store.tasks.length" class="empty-state">还没有任务，先创建一个安全分析任务。</p>
      <table v-else>
        <thead>
          <tr><th>任务</th><th>场景</th><th>路由</th><th>状态</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="task in store.tasks" :key="task.id">
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
