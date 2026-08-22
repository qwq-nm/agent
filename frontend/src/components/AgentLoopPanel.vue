<script setup lang="ts">
import { computed } from 'vue'
import type { TaskDetail } from '../types'
import { providerName, stageLabel, statusLabel, toolNameLabel } from '../labels'

const props = defineProps<{
  task: TaskDetail
}>()

const recentModelCalls = computed(() => props.task.model_calls.slice(-8))
const recentToolCalls = computed(() => props.task.tool_calls.slice(-8))

function toolSummary(result: Record<string, unknown>) {
  const summary = result.summary
  return typeof summary === 'string' && summary ? summary : '暂无摘要'
}
</script>

<template>
  <section class="panel agent-loop-panel">
    <header class="panel-title compact-title">
      <div>
        <p class="eyebrow">AGENT LOOP</p>
        <h2>AI 决策与工具协同</h2>
      </div>
      <span>{{ task.model_calls.length }} 次模型节点 / {{ task.tool_calls.length }} 次工具调用</span>
    </header>

    <div class="agent-loop-grid">
      <article>
        <h3>AI 在做什么</h3>
        <p>
          AI 负责理解任务、生成计划、复核证据和生成报告；真正访问 URL 或解析文件的是白名单工具。
        </p>
        <ol v-if="recentModelCalls.length" class="loop-list">
          <li v-for="call in recentModelCalls" :key="call.id">
            <strong>{{ stageLabel(call.stage) }}</strong>
            <span>{{ providerName(call.provider) }} · {{ statusLabel(call.status) }}</span>
          </li>
        </ol>
        <p v-else class="muted-text">暂无模型调用记录。</p>
      </article>

      <article>
        <h3>工具调用起到什么作用</h3>
        <p>
          工具输出会写入证据账本，并在下一轮复核或重规划时作为 AI 的上下文。
        </p>
        <ol v-if="recentToolCalls.length" class="loop-list tool-loop-list">
          <li v-for="call in recentToolCalls" :key="call.id">
            <strong>{{ call.step_name || toolNameLabel(call.tool_name) }}</strong>
            <span>{{ toolNameLabel(call.tool_name) }} · {{ statusLabel(call.status) }}</span>
            <small v-if="call.step_purpose">调用原因：{{ call.step_purpose }}</small>
            <small>关键结果：{{ toolSummary(call.result) }}</small>
          </li>
        </ol>
        <p v-else class="muted-text">暂无工具调用记录。</p>
      </article>
    </div>
  </section>
</template>
