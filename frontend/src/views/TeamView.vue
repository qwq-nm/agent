<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../api/client'
import UserDialog from '../components/UserDialog.vue'
import type { AdminUser, UserRole } from '../types'

const users = ref<AdminUser[]>([])
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const dialogOpen = ref(false)
const editingUser = ref<AdminUser | null>(null)
const pendingDisable = ref<AdminUser | null>(null)
const confirmation = ref('')
const activeAdmins = computed(() => users.value.filter((user) => user.role === 'admin' && user.is_active).length)

async function load() {
  loading.value = true
  error.value = ''
  try { users.value = await api.listUsers() }
  catch (value) { error.value = value instanceof Error ? value.message : '成员列表加载失败' }
  finally { loading.value = false }
}

function openCreate() {
  editingUser.value = null
  dialogOpen.value = true
}

function openPasswordReset(user: AdminUser) {
  editingUser.value = user
  dialogOpen.value = true
}

function requestDisable(user: AdminUser) {
  if (user.role === 'admin' && user.is_active && activeAdmins.value <= 1) {
    error.value = '不能停用最后一名管理员'
    return
  }
  error.value = ''
  pendingDisable.value = user
  confirmation.value = ''
}

function cancelDisable() {
  pendingDisable.value = null
  confirmation.value = ''
}

async function save(payload: { username?: string; password: string; role?: UserRole }) {
  busy.value = true
  error.value = ''
  try {
    if (editingUser.value) await api.updateUser(editingUser.value.id, { password: payload.password })
    else await api.createUser({ username: payload.username!, password: payload.password, role: payload.role! })
    dialogOpen.value = false
    await load()
  } catch (value) { error.value = value instanceof Error ? value.message : '成员更新失败' }
  finally { busy.value = false }
}

async function setActive(user: AdminUser, isActive: boolean): Promise<boolean> {
  if (!isActive && user.role === 'admin' && user.is_active && activeAdmins.value <= 1) {
    error.value = '不能停用最后一名管理员'
    return false
  }
  busy.value = true
  error.value = ''
  try {
    await api.updateUser(user.id, { is_active: isActive })
    await load()
    return true
  } catch (value) { error.value = value instanceof Error ? value.message : '成员状态更新失败' }
  finally { busy.value = false }
  return false
}

async function confirmDisable() {
  const user = pendingDisable.value
  if (!user || confirmation.value !== user.username) return
  if (await setActive(user, false)) cancelDisable()
}

onMounted(load)
</script>

<template>
  <section class="page">
    <header class="page-header"><div><p class="eyebrow">TEAM ADMINISTRATION</p><h1>团队成员</h1><p>管理本地账号、角色和会话状态；密码只在提交时传输，不会显示或写入界面。</p></div><button class="primary-button" data-action="new-user" type="button" @click="openCreate">新增成员</button></header>
    <p v-if="error" class="error-message" role="alert">{{ error }}</p>
    <div class="panel table-panel admin-table">
      <p v-if="loading" class="empty-state">正在加载成员…</p>
      <table v-else>
        <thead><tr><th>成员</th><th>角色</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="user in users" :key="user.id">
            <td><strong>{{ user.username }}</strong><small>{{ user.id.slice(0, 8) }}</small></td>
            <td>{{ user.role === 'admin' ? '管理员' : '分析员' }}</td>
            <td><span class="status-badge" :data-status="user.is_active ? 'active' : 'disabled'">{{ user.is_active ? '启用' : '停用' }}</span></td>
            <td class="admin-actions"><button v-if="!user.is_active" class="ghost-button" :data-action="`enable-${user.id}`" type="button" :disabled="busy" @click="setActive(user, true)">启用</button><button v-else class="ghost-button danger" :data-action="`disable-${user.id}`" type="button" :disabled="busy" @click="requestDisable(user)">停用</button><button class="ghost-button" :data-action="`reset-${user.id}`" type="button" :disabled="busy" @click="openPasswordReset(user)">重置密码</button></td>
          </tr>
        </tbody>
      </table>
    </div>
    <UserDialog :open="dialogOpen" :user="editingUser" :busy="busy" @close="dialogOpen = false" @submit="save" />
    <div v-if="pendingDisable" class="dialog-backdrop" role="presentation">
      <form class="admin-dialog panel" data-form="disable-user" @submit.prevent="confirmDisable">
        <header class="panel-title"><div><p class="eyebrow">DISRUPTIVE CHANGE</p><h2>停用成员</h2></div><button type="button" class="icon-button" aria-label="关闭" @click="cancelDisable">×</button></header>
        <div class="admin-dialog-body"><p class="dialog-context">停用后，{{ pendingDisable.username }} 将无法登录。请输入成员名 <code>{{ pendingDisable.username }}</code> 以确认此操作。</p><label class="field"><span>确认成员名</span><input v-model="confirmation" name="confirmation" :placeholder="pendingDisable.username" autocomplete="off" required></label></div>
        <footer class="dialog-actions"><button type="button" class="ghost-button" @click="cancelDisable">取消</button><button class="ghost-button danger" data-action="confirm-disable" type="submit" :disabled="busy || confirmation !== pendingDisable.username">确认停用</button></footer>
      </form>
    </div>
  </section>
</template>
