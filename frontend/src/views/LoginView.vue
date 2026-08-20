<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { safeRedirectPath } from '../router'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const username = ref('')
const password = ref('')
const error = ref('')
const submitting = ref(false)

async function submit() {
  submitting.value = true
  error.value = ''
  try {
    await auth.login(username.value, password.value)
    password.value = ''
    await router.replace(safeRedirectPath(route.query.redirect))
  } catch (value) {
    error.value = value instanceof Error ? value.message : 'Request failed'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <main class="login-page">
    <form class="login-card panel" @submit.prevent="submit">
      <span class="brand-mark">SX</span>
      <p class="eyebrow">SECAGENT-X</p>
      <h1>Sign in</h1>
      <label class="field"><span>Username</span><input v-model="username" autocomplete="username" required></label>
      <label class="field"><span>Password</span><input v-model="password" type="password" autocomplete="current-password" required></label>
      <p v-if="error" class="error-message" role="alert">{{ error }}</p>
      <button class="primary-button" :disabled="submitting" type="submit">{{ submitting ? 'Signing in…' : 'Sign in' }}</button>
    </form>
  </main>
</template>
