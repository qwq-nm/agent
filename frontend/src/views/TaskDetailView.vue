<script setup lang="ts">
import { useRoute } from 'vue-router'
const route = useRoute()
</script>

<template>
  <section class="page"><div class="panel empty-state">正在加载任务 {{ route.params.id }} 的执行详情…</div></section>
</template>
