<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from './stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const items = [
  { to: '/', label: '总览', icon: '◫' },
  { to: '/chat/new', label: 'AI 对话', icon: '✦' },
  { to: '/tasks/new', label: '创建任务', icon: '+' },
  { to: '/tasks', label: '任务中心', icon: '◎' },
  { to: '/reports', label: '报告', icon: '≣' },
  { to: '/system', label: '模型与工具', icon: '◇', admin: true },
  { to: '/team', label: 'Team', icon: '◉', admin: true },
  { to: '/audit', label: 'Audit', icon: '◉', admin: true },
]
const visibleItems = computed(() => items.filter((item) => !item.admin || auth.user?.role === 'admin'))

async function signOut() {
  await auth.logout()
  await router.replace('/login')
}
</script>

<template>
  <RouterView v-if="route.path === '/login'" />
  <div v-else class="app-shell">
    <aside class="sidebar">
      <RouterLink class="brand" to="/">
        <span class="brand-mark">SX</span>
        <span><strong>SecAgent-X</strong><small>AUTONOMOUS DEFENSE</small></span>
      </RouterLink>
      <nav>
        <RouterLink
          v-for="item in visibleItems"
          :key="item.to"
          :to="item.to"
          :class="{ active: route.path === item.to || (item.to === '/tasks' && route.path.startsWith('/tasks/')) }"
        >
          <i>{{ item.icon }}</i><span>{{ item.label }}</span>
        </RouterLink>
      </nav>
      <div class="sidebar-note">
        <span class="pulse"></span>
        <div><strong>安全边界已启用</strong><small>SSRF / 文件隔离 / 风险门禁</small></div>
      </div>
    </aside>
    <main class="main-content">
      <header class="topbar">
        <span>SecAgent-X 控制台</span>
        <div><span class="online-dot"></span> API 连接状态由页面实时检测 <button class="link-button" type="button" @click="signOut">退出</button></div>
      </header>
      <RouterView />
    </main>
  </div>
</template>
