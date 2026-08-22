<script setup lang="ts">
import { ref, watch } from 'vue'
import type { PlanPreview } from '../types'
import { riskLabel, safetyModeLabel, sceneLabel, toolNameLabel } from '../labels'

const props = defineProps<{
  preview?: PlanPreview | null
  canStart?: boolean
  busy?: boolean
  defaultCollapsed?: boolean
}>()

const emit = defineEmits<{ start: [] }>()
const collapsed = ref(Boolean(props.defaultCollapsed))

watch(
  () => props.defaultCollapsed,
  (value) => {
    collapsed.value = Boolean(value)
  },
)
</script>

<template>
  <section v-if="preview" class="panel plan-preview" :class="{ collapsed }">
    <header class="panel-title">
      <div>
        <p class="eyebrow">PLAN PREVIEW</p>
        <h2>执行计划预览</h2>
        <small>共 {{ preview.steps.length }} 步 · {{ sceneLabel(preview.scene) }} · {{ safetyModeLabel(preview.safety_mode) }}</small>
      </div>
      <div class="plan-actions">
        <button class="ghost-button" type="button" @click="collapsed = !collapsed">
          {{ collapsed ? '展开计划' : '收起计划' }}
        </button>
        <button v-if="canStart" class="primary-button" :disabled="busy" @click="emit('start')">
          确认并开始执行
        </button>
      </div>
    </header>

    <div v-if="collapsed" class="plan-collapsed-summary">
      <strong>{{ preview.goal_summary }}</strong>
      <p>执行开始后计划会默认收起，方便优先查看当前阶段、待审批动作和执行时间线。</p>
    </div>

    <template v-else>
      <div class="plan-understanding">
        <article>
          <span>任务类型</span>
          <strong>{{ sceneLabel(preview.scene) }}</strong>
        </article>
        <article>
          <span>安全策略</span>
          <strong>{{ safetyModeLabel(preview.safety_mode) }}</strong>
        </article>
        <article v-if="preview.target_summary">
          <span>目标</span>
          <strong>{{ preview.target_summary }}</strong>
        </article>
      </div>

      <div class="plan-summary">
        <article>
          <h3>AI 理解结果</h3>
          <p>{{ preview.goal_summary }}</p>
        </article>
        <article>
          <h3>授权边界</h3>
          <p>{{ preview.authorization_summary }}</p>
        </article>
      </div>

      <div v-if="preview.constraints.length || preview.expected_outputs.length" class="plan-lists">
        <article v-if="preview.constraints.length">
          <h3>限制条件</h3>
          <ul>
            <li v-for="item in preview.constraints" :key="item">{{ item }}</li>
          </ul>
        </article>
        <article v-if="preview.expected_outputs.length">
          <h3>预期输出</h3>
          <ul>
            <li v-for="item in preview.expected_outputs" :key="item">{{ item }}</li>
          </ul>
        </article>
      </div>

      <ol class="plan-step-list">
        <li v-for="step in preview.steps" :key="`${step.index}-${step.name}`">
          <span class="plan-index">{{ step.index }}</span>
          <div>
            <header>
              <strong>{{ step.name }}</strong>
              <small>{{ toolNameLabel(step.tool_name) }}</small>
            </header>
            <p>{{ step.purpose }}</p>
            <dl>
              <div>
                <dt>风险</dt>
                <dd>{{ riskLabel(step.risk_level) }}</dd>
              </div>
              <div>
                <dt>人工确认</dt>
                <dd>{{ step.need_human_confirm ? '需要' : '不需要' }}</dd>
              </div>
              <div>
                <dt>参数摘要</dt>
                <dd><code>{{ JSON.stringify(step.params || {}) }}</code></dd>
              </div>
            </dl>
          </div>
        </li>
      </ol>
    </template>
  </section>
</template>
