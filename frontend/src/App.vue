<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElButton, ElTag } from 'element-plus'

type Connection = 'checking' | 'connected' | 'unavailable'

const connection = ref<Connection>('checking')
let activeRequest: AbortController | undefined

const statusText = computed(() => ({
  checking: '正在检查连接',
  connected: '服务已连接',
  unavailable: '暂时无法连接',
})[connection.value])

const statusType = computed(() => ({
  checking: 'info',
  connected: 'success',
  unavailable: 'danger',
})[connection.value] as 'info' | 'success' | 'danger')

async function checkConnection() {
  activeRequest?.abort()
  const request = new AbortController()
  activeRequest = request
  connection.value = 'checking'
  const timeout = window.setTimeout(() => request.abort(), 5000)

  try {
    const response = await fetch('/api/health', {
      signal: request.signal,
      cache: 'no-store',
    })
    const result: unknown = await response.json()
    if (!response.ok || typeof result !== 'object' || result === null
      || !('status' in result) || result.status !== 'ok') {
      throw new Error('Service unavailable')
    }
    if (activeRequest === request) connection.value = 'connected'
  } catch {
    if (activeRequest === request) connection.value = 'unavailable'
  } finally {
    window.clearTimeout(timeout)
  }
}

onMounted(checkConnection)
onBeforeUnmount(() => {
  activeRequest?.abort()
  activeRequest = undefined
})
</script>

<template>
  <div class="workspace">
    <header class="workspace-header">
      <a class="wordmark" href="/" aria-label="Team.s 首页">Team.s<span>v2</span></a>
      <p class="workspace-label">科研协同工作台</p>
    </header>

    <main class="workspace-main">
      <div class="workspace-intro">
        <p class="eyebrow">人 + 智能体 · 共同协作</p>
        <h1>让每一次交接都有据可循。</h1>
        <p class="intro-description">围绕研究任务，组织分工、交付与审核。</p>
      </div>

      <section class="connection-panel" aria-labelledby="connection-heading">
        <div class="panel-copy">
          <h2 id="connection-heading">工作台准备中</h2>
          <p>账号与项目功能将陆续开放。</p>
        </div>
        <div class="connection-actions">
          <span role="status" aria-live="polite">
            <ElTag :type="statusType" effect="plain" round>{{ statusText }}</ElTag>
          </span>
          <ElButton :loading="connection === 'checking'" @click="checkConnection">
            重新检查
          </ElButton>
        </div>
        <p v-if="connection === 'unavailable'" class="connection-help">
          请稍后重新检查，或联系工作台管理员。
        </p>
      </section>
    </main>

    <footer class="workspace-footer">Team.s · 人负责验证，协作留下记录。</footer>
  </div>
</template>
