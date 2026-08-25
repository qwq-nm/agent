<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from './stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const theme = ref<'dark' | 'light'>('dark')
const sidebarCollapsed = ref(false)
const tourOpen = ref(false)
const tourStep = ref(0)
const tourRect = ref<DOMRect | null>(null)
const items = [
  { to: '/', label: '总览', icon: '◫' },
  { to: '/tasks/new', label: '创建任务', icon: '+' },
  { to: '/tasks', label: '任务中心', icon: '◎' },
  { to: '/reports', label: '报告', icon: '≣' },
  { to: '/system', label: '模型与工具', icon: '◇', admin: true },
  { to: '/team', label: '团队管理', icon: '◉', admin: true },
  { to: '/audit', label: '审计记录', icon: '◉', admin: true },
]
const visibleItems = computed(() => items.filter((item) => !item.admin || auth.user?.role === 'admin'))

const tourSteps = computed(() => {
  const common = [{ target: '[data-tour="sidebar"]', title: '从这里开始', text: '通过侧边栏切换总览、创建任务、任务中心和安全报告。' }]
  if (route.path === '/tasks/new') return [...common, { target: '[data-tour="task-prompt"]', title: '描述任务', text: '先写清目标、授权范围和限制条件，Agent 会据此规划任务。' }, { target: '.template-grid', title: '快速开始', text: '也可以选择模板，自动填充任务建议和分析模式。' }, { target: '[data-tour="task-config"]', title: '配置并执行', text: '在这里选择任务模式和安全策略，最后生成执行计划。' }]
  if (route.path === '/reports' || route.path.startsWith('/reports')) return [...common, { target: '[data-tour="report-list"]', title: '报告导览', text: '从右侧选择已完成任务，查看对应的安全分析报告。' }, { target: '[data-tour="report-document"]', title: '阅读证据链', text: '报告正文和证据链会保留文件、工具和分析结论，便于回溯。' }]
  return common
})
const currentTourStep = computed(() => tourSteps.value[tourStep.value])
const tourStyle = computed(() => {
  const rect = tourRect.value
  if (!rect) return {}
  const cardHeight = Math.min(230, window.innerHeight - 32)
  const below = rect.bottom + 14
  const above = rect.top - cardHeight - 14
  const top = below + cardHeight <= window.innerHeight - 16 ? below : Math.max(16, above)
  const left = Math.min(window.innerWidth - 330, Math.max(16, rect.left))
  return { top: `${top}px`, left: `${left}px` }
})
function updateTourPosition() {
  if (!tourOpen.value || !currentTourStep.value) return
  const target = document.querySelector(currentTourStep.value.target)
  tourRect.value = target?.getBoundingClientRect() || null
}
function openTour() { tourStep.value = 0; tourOpen.value = true; nextTick(updateTourPosition) }
function closeTour() { tourOpen.value = false; localStorage.setItem('secagent-tour-complete', 'true') }
function nextTourStep() { if (tourStep.value >= tourSteps.value.length - 1) closeTour(); else { tourStep.value += 1; nextTick(updateTourPosition) } }
function previousTourStep() { if (tourStep.value > 0) { tourStep.value -= 1; nextTick(updateTourPosition) } }

function applyTheme(value: 'dark' | 'light') {
  theme.value = value
  document.documentElement.dataset.theme = value
  localStorage.setItem('secagent-theme', value)
}

function toggleTheme() {
  applyTheme(theme.value === 'dark' ? 'light' : 'dark')
}

function toggleSidebar() {
  sidebarCollapsed.value = !sidebarCollapsed.value
  localStorage.setItem('secagent-sidebar-collapsed', String(sidebarCollapsed.value))
}

onMounted(() => {
  const saved = localStorage.getItem('secagent-theme')
  applyTheme(saved === 'light' ? 'light' : 'dark')
  sidebarCollapsed.value = localStorage.getItem('secagent-sidebar-collapsed') === 'true'
  if (!localStorage.getItem('secagent-tour-complete')) window.setTimeout(openTour, 500)
  window.addEventListener('resize', updateTourPosition)
  window.addEventListener('scroll', updateTourPosition, true)
})

watch(() => route.path, () => { if (tourOpen.value) { tourStep.value = 0; nextTick(updateTourPosition) } })
watch(currentTourStep, () => nextTick(updateTourPosition))
onBeforeUnmount(() => { window.removeEventListener('resize', updateTourPosition); window.removeEventListener('scroll', updateTourPosition, true) })

async function signOut() {
  await auth.logout()
  await router.replace('/login')
}
</script>

<template>
  <RouterView v-if="route.path === '/login'" />
  <div v-else class="app-shell" :class="{ 'is-sidebar-collapsed': sidebarCollapsed }">
    <aside class="sidebar">
      <RouterLink class="brand" to="/">
        <span class="brand-mark">SX</span>
        <span><strong>SecAgent-X</strong><small>AUTONOMOUS DEFENSE</small></span>
      </RouterLink>
      <button class="sidebar-toggle" type="button" :aria-label="sidebarCollapsed ? '展开侧栏' : '收起侧栏'" :title="sidebarCollapsed ? '展开侧栏' : '收起侧栏'" @click="toggleSidebar">
        <span aria-hidden="true">{{ sidebarCollapsed ? '›' : '‹' }}</span>
        <span class="sidebar-toggle-label">{{ sidebarCollapsed ? '展开' : '收起侧栏' }}</span>
      </button>
      <nav data-tour="sidebar">
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
        <div class="topbar-title"><span class="topbar-kicker">SECAGENT-X</span><span>控制台</span></div>
        <div class="topbar-actions">
          <button class="tour-launch" type="button" @click="openTour">使用导览</button>
          <span class="connection-state"><span class="online-dot"></span>API 在线</span>
          <button class="theme-toggle" type="button" :aria-label="theme === 'dark' ? '切换亮色主题' : '切换暗色主题'" @click="toggleTheme">
            <span aria-hidden="true">{{ theme === 'dark' ? '☀' : '☾' }}</span>
            {{ theme === 'dark' ? '亮色' : '暗色' }}
          </button>
          <button class="link-button" type="button" @click="signOut">退出</button>
        </div>
      </header>
      <RouterView />
    </main>
    <div v-if="tourOpen" class="product-tour" role="dialog" aria-modal="true" aria-label="使用导览">
      <div class="tour-backdrop" @click="closeTour"></div>
      <div v-if="tourRect" class="tour-spotlight" :style="{ top: `${tourRect.top - 6}px`, left: `${tourRect.left - 6}px`, width: `${tourRect.width + 12}px`, height: `${tourRect.height + 12}px` }"></div>
      <section class="tour-card" :style="tourStyle">
        <div class="tour-card-header"><span>使用导览 · {{ tourStep + 1 }}/{{ tourSteps.length }}</span><button type="button" @click="closeTour">跳过</button></div>
        <h2>{{ currentTourStep?.title }}</h2>
        <p>{{ currentTourStep?.text }}</p>
        <div class="tour-actions"><button v-if="tourStep > 0" type="button" class="tour-prev" @click="previousTourStep">上一步</button><button type="button" class="tour-next" @click="nextTourStep">{{ tourStep === tourSteps.length - 1 ? '完成' : '下一步' }}</button></div>
      </section>
    </div>
  </div>
</template>
