<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { AdminUser, UserRole } from '../types'

const props = withDefaults(defineProps<{
  open: boolean
  user?: AdminUser | null
  busy?: boolean
}>(), { user: null, busy: false })

const emit = defineEmits<{
  close: []
  submit: [payload: { username?: string; password: string; role?: UserRole }]
}>()

const username = ref('')
const password = ref('')
const role = ref<UserRole>('analyst')
const editing = computed(() => Boolean(props.user))

watch(() => [props.open, props.user], () => {
  username.value = props.user?.username || ''
  role.value = props.user?.role || 'analyst'
  password.value = ''
}, { immediate: true })

function submit() {
  emit('submit', {
    ...(editing.value ? {} : { username: username.value, role: role.value }),
    password: password.value,
  })
}
</script>

<template>
  <div v-if="open" class="dialog-backdrop" role="presentation">
    <form class="admin-dialog panel" data-form="user" @submit.prevent="submit">
      <header class="panel-title">
        <div><p class="eyebrow">TEAM ACCESS</p><h2>{{ editing ? '重置密码' : '新增成员' }}</h2></div>
        <button type="button" class="icon-button" aria-label="关闭" @click="emit('close')">×</button>
      </header>
      <div class="admin-dialog-body">
        <label v-if="!editing" class="field"><span>用户名</span><input v-model="username" name="username" minlength="3" maxlength="80" required autocomplete="off"></label>
        <p v-else class="dialog-context">正在更新 <strong>{{ user?.username }}</strong> 的访问凭据。</p>
        <label class="field"><span>{{ editing ? '新密码' : '初始密码' }}</span><input v-model="password" name="password" type="password" minlength="8" maxlength="256" required autocomplete="new-password"></label>
        <label v-if="!editing" class="field"><span>角色</span><select v-model="role" name="role"><option value="analyst">分析员</option><option value="admin">管理员</option></select></label>
      </div>
      <footer class="dialog-actions"><button type="button" class="ghost-button" @click="emit('close')">取消</button><button class="primary-button" type="submit" :disabled="busy">{{ busy ? '处理中…' : '确认' }}</button></footer>
    </form>
  </div>
</template>
