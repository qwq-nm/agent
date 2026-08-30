import { createRouter, createWebHistory } from 'vue-router'
import DashboardView from './views/DashboardView.vue'
import TaskCreateView from './views/TaskCreateView.vue'
import TaskDetailView from './views/TaskDetailView.vue'
import TaskListView from './views/TaskListView.vue'
import ReportsView from './views/ReportsView.vue'
import SystemView from './views/SystemView.vue'
import TeamView from './views/TeamView.vue'
import AuditView from './views/AuditView.vue'
import LoginView from './views/LoginView.vue'
import ChatView from './views/ChatView.vue'
import { useAuthStore } from './stores/auth'

declare module 'vue-router' {
  interface RouteMeta {
    requiresAuth?: boolean
    requiresAdmin?: boolean
  }
}

export function safeRedirectPath(value: unknown): string {
  if (typeof value !== 'string') return '/'

  let normalized = value
  for (let round = 0; round < 4; round += 1) {
    try {
      const decoded = decodeURIComponent(normalized)
      if (decoded === normalized) break
      normalized = decoded
    } catch {
      return '/'
    }
    if (round === 3) return '/'
  }

  normalized = normalized.replace(/\\/g, '/')
  if (
    !normalized.startsWith('/') ||
    normalized.startsWith('//') ||
    /[\u0000-\u001F\u007F]/.test(normalized) ||
    /^[a-z][a-z\d+.-]*:/i.test(normalized)
  ) return '/'
  return value
}

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', component: LoginView },
    { path: '/', component: DashboardView, meta: { requiresAuth: true } },
    { path: '/chat/new', component: ChatView, meta: { requiresAuth: true } },
    { path: '/chat/:conversationId', component: ChatView, meta: { requiresAuth: true } },
    { path: '/tasks/new', redirect: '/chat/new' },
    { path: '/tasks', component: TaskListView, meta: { requiresAuth: true } },
    { path: '/tasks/:id', component: TaskDetailView, meta: { requiresAuth: true } },
    { path: '/reports', component: ReportsView, meta: { requiresAuth: true } },
    { path: '/system', component: SystemView, meta: { requiresAuth: true, requiresAdmin: true } },
    { path: '/team', component: TeamView, meta: { requiresAuth: true, requiresAdmin: true } },
    { path: '/audit', component: AuditView, meta: { requiresAuth: true, requiresAdmin: true } },
  ],
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  await auth.bootstrap()
  if (to.path === '/login') return auth.user && auth.accessToken ? safeRedirectPath(to.query.redirect) : true
  if (!to.meta.requiresAuth) return true
  if (!auth.user || !auth.accessToken) return { path: '/login', query: { redirect: safeRedirectPath(to.fullPath) } }
  if (to.meta.requiresAdmin && auth.user.role !== 'admin') return '/'
  return true
})

window.addEventListener('secagent:auth-expired', () => {
  if (router.currentRoute.value.path !== '/login') void router.replace({ path: '/login' })
})
