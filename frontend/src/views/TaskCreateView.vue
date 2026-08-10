<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api/client'
import type { RouteMode } from '../types'

const router = useRouter()
const submitting = ref(false)
const error = ref('')
const upload = ref<File>()
const form = reactive({
  goal: '',
  authorization_scope: '',
  route_mode: 'auto' as RouteMode,
  preferred_model: '',
  scene_hint: '',
  target_url: '',
})

async function submit() {
  submitting.value = true
  error.value = ''
  try {
    const task = await api.createTask(
      {
        ...form,
        preferred_model: form.preferred_model || undefined,
        scene_hint: form.scene_hint || undefined,
        target_url: form.target_url || undefined,
      },
      upload.value,
    )
    await router.push(`/tasks/${task.id}`)
  } catch (value) {
    error.value = value instanceof Error ? value.message : '创建任务失败'
  } finally {
    submitting.value = false
  }
}

function chooseFile(event: Event) {
  upload.value = (event.target as HTMLInputElement).files?.[0]
}
</script>

<template>
  <section class="page narrow-page">
    <header class="page-header">
      <div>
        <p class="eyebrow">NEW SECURITY MISSION</p>
        <h1>创建安全任务</h1>
        <p>明确目标与授权边界，系统将自动选择 GLM 或 DeepSeek 完成不同阶段。</p>
      </div>
      <span class="safety-chip">白名单工具执行</span>
    </header>

    <form class="mission-form panel" @submit.prevent="submit">
      <div class="form-section">
        <span class="step-number">01</span>
        <div>
          <h2>目标与授权</h2>
          <p>所有工具调用都必须落在这里声明的授权范围内。</p>
        </div>
      </div>
      <label class="field full">
        <span>任务目标</span>
        <textarea
          v-model="form.goal"
          data-test="goal"
          required
          minlength="3"
          placeholder="例如：分析 access.log 中的异常扫描并生成证据报告"
        />
      </label>
      <label class="field full">
        <span>授权范围</span>
        <textarea
          v-model="form.authorization_scope"
          data-test="authorization"
          required
          minlength="3"
          placeholder="例如：仅分析本次上传文件，不执行任何外部命令"
        />
      </label>

      <div class="form-grid">
        <label class="field">
          <span>场景</span>
          <select v-model="form.scene_hint">
            <option value="">自动识别</option>
            <option value="incident_response">日志应急响应</option>
            <option value="source_audit">静态源码审计</option>
            <option value="web_analysis">被动 Web 分析</option>
          </select>
        </label>
        <label class="field">
          <span>模型路由</span>
          <select v-model="form.route_mode">
            <option value="auto">自动协同</option>
            <option value="manual">手动指定</option>
          </select>
        </label>
        <label v-if="form.route_mode === 'manual'" class="field">
          <span>指定模型</span>
          <select v-model="form.preferred_model" required>
            <option disabled value="">请选择</option>
            <option value="deepseek">DeepSeek</option>
            <option value="glm">GLM</option>
          </select>
        </label>
        <label class="field">
          <span>授权 URL（Web 场景）</span>
          <input v-model="form.target_url" type="url" placeholder="https://example.com">
        </label>
      </div>

      <label class="upload-zone">
        <input aria-label="上传材料" type="file" @change="chooseFile">
        <span class="upload-icon">↑</span>
        <strong>{{ upload?.name || '选择日志、源码或 ZIP 材料' }}</strong>
        <small>文件将进入任务独立工作区；源码只做静态读取。</small>
      </label>

      <p v-if="error" class="error-message" role="alert">{{ error }}</p>
      <div class="form-actions">
        <span>高风险与禁用动作不会执行</span>
        <button class="primary-button" :disabled="submitting" type="submit">
          {{ submitting ? '正在创建…' : '创建任务' }}
        </button>
      </div>
    </form>
  </section>
</template>
