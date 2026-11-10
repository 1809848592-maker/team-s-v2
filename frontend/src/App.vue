<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElButton, ElConfigProvider, ElInput, ElTag } from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { AuthError, getAccount, signIn, signOut } from './api/auth'
import type { Account } from './api/auth'
import ProjectWorkspace from './components/ProjectWorkspace.vue'

type Connection = 'checking' | 'connected' | 'unavailable'

const connection = ref<Connection>('checking')
let activeRequest: AbortController | undefined
const account = ref<Account | null>(null)
const sessionState = ref<'loading' | 'ready' | 'unavailable'>('loading')
const mode = ref<'login' | 'register'>('login')
const username = ref('')
const password = ref('')
const busy = ref(false)
const authError = ref('')
const notice = ref('')

function errorMessage(error: unknown, action: 'login' | 'register' | 'session' | 'logout'): string {
  if (!(error instanceof AuthError)) return '暂时无法连接服务，请检查网络后重试。'
  if (error.status === 401) {
    return action === 'login' ? '用户名或密码不正确。' : '登录已过期，请重新登录。'
  }
  if (error.status === 409) return '这个用户名或邮箱已被使用，请直接登录或更换名称。'
  if (error.status === 422) return '请检查用户名与密码是否符合要求。'
  if (error.status === 403) return '请求未能验证，请刷新页面后重试。'
  if (error.status === 429) return '操作过于频繁，请稍后重试。'
  return '操作未完成，请稍后重试。'
}

function returnToLogin() {
  account.value = null
  mode.value = 'login'
  password.value = ''
  sessionState.value = 'ready'
}

async function loadSession() {
  sessionState.value = 'loading'
  authError.value = ''
  notice.value = ''
  try {
    account.value = await getAccount()
    sessionState.value = 'ready'
  } catch (error) {
    if (error instanceof AuthError && error.status === 401) {
      returnToLogin()
    } else {
      sessionState.value = 'unavailable'
      authError.value = errorMessage(error, 'session')
    }
  }
}

function switchMode() {
  mode.value = mode.value === 'login' ? 'register' : 'login'
  password.value = ''
  authError.value = ''
  notice.value = ''
}

async function submitAccount() {
  if (busy.value) return
  authError.value = ''
  notice.value = ''
  username.value = username.value.trim()
  if (!username.value || username.value.length > 80) {
    authError.value = '请输入用户名或邮箱，长度为 1–80 个字符。'
    return
  }
  if (password.value.length < 8 || password.value.length > 256) {
    authError.value = '密码长度应为 8–256 个字符。'
    return
  }
  busy.value = true
  try {
    account.value = await signIn(mode.value, username.value, password.value)
    notice.value = mode.value === 'register' ? '账号已创建，已登录。' : '已登录。'
    password.value = ''
  } catch (error) {
    authError.value = errorMessage(error, mode.value)
    if (error instanceof AuthError && error.status === 401 && mode.value !== 'login') {
      returnToLogin()
    }
  } finally {
    busy.value = false
  }
}

async function logout() {
  if (busy.value) return
  busy.value = true
  authError.value = ''
  notice.value = ''
  try {
    await signOut()
    returnToLogin()
    notice.value = '已退出登录。'
  } catch (error) {
    if (error instanceof AuthError && error.status === 401) returnToLogin()
    authError.value = errorMessage(error, 'logout')
  } finally {
    busy.value = false
  }
}

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
      credentials: 'same-origin',
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

function sessionExpired() { returnToLogin(); authError.value = '登录已过期，请重新登录。' }
onMounted(() => {
  window.addEventListener('session-expired', sessionExpired)
  void checkConnection()
  void loadSession()
})
onBeforeUnmount(() => {
  window.removeEventListener('session-expired', sessionExpired)
  activeRequest?.abort()
  activeRequest = undefined
})
</script>

<template>
  <ElConfigProvider :locale="zhCn">
  <ProjectWorkspace v-if="account && sessionState === 'ready'" :account="account" :busy="busy" :logout-error="authError" @logout="logout" />
  <div v-else class="workspace">
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

      <section class="account-panel" aria-labelledby="account-heading" :aria-busy="sessionState === 'loading' || busy">
        <template v-if="sessionState === 'loading'">
          <h2 id="account-heading">正在检查登录状态</h2>
          <p class="panel-description" role="status">请稍候。</p>
        </template>

        <template v-else-if="sessionState === 'unavailable'">
          <h2 id="account-heading">暂时无法检查登录状态</h2>
          <p class="form-message error-message" role="alert">{{ authError }}</p>
          <ElButton @click="loadSession">重新检查登录状态</ElButton>
        </template>

        <template v-else>
          <template v-if="account">
            <div class="account-heading-row">
              <h2 id="account-heading">已登录</h2>
              <ElTag v-if="account.is_admin" effect="plain">平台管理员</ElTag>
            </div>
            <p class="account-name">{{ account.username }}</p>
            <p class="panel-description">项目功能将陆续开放。</p>
            <ElButton :loading="busy" @click="logout">退出登录</ElButton>
          </template>

          <template v-else>
            <h2 id="account-heading">{{ mode === 'login' ? '登录工作台' : '创建账号' }}</h2>
            <p class="panel-description">{{ mode === 'login' ? '使用你的账号继续协作。' : '注册后即可登录工作台。' }}</p>
            <form class="account-form" novalidate @submit.prevent="submitAccount">
              <div class="form-field">
                <label for="account-username">用户名或邮箱</label>
                <ElInput id="account-username" v-model="username" name="username" autocomplete="username"
                  :maxlength="80" :disabled="busy" required />
              </div>
              <div class="form-field">
                <label for="account-password">密码</label>
                <ElInput id="account-password" :key="mode" v-model="password" name="password" type="password"
                  :autocomplete="mode === 'login' ? 'current-password' : 'new-password'"
                  :minlength="8" :maxlength="256" :disabled="busy" aria-describedby="password-hint"
                  show-password required />
                <p id="password-hint" class="field-hint">8–256 个字符</p>
              </div>
              <ElButton type="primary" native-type="submit" :loading="busy" class="submit-button">
                {{ mode === 'login' ? '登录' : '注册并登录' }}
              </ElButton>
            </form>
            <p class="mode-switch">
              {{ mode === 'login' ? '还没有账号？' : '已有账号？' }}
              <ElButton link type="primary" :disabled="busy" @click="switchMode">
                {{ mode === 'login' ? '创建账号' : '返回登录' }}
              </ElButton>
            </p>
          </template>

          <p v-if="authError" class="form-message error-message" role="alert">{{ authError }}</p>
          <p v-if="notice" class="form-message success-message" role="status">{{ notice }}</p>
        </template>
      </section>

      <section class="connection-panel" aria-label="服务连接状态">
        <div class="connection-actions">
          <span role="status" aria-live="polite">
            <ElTag :type="statusType" effect="plain" round>{{ statusText }}</ElTag>
          </span>
          <ElButton link :loading="connection === 'checking'" @click="checkConnection">
            重新检查连接
          </ElButton>
        </div>
        <p v-if="connection === 'unavailable'" class="connection-help">
          请稍后重新检查，或联系工作台管理员。
        </p>
      </section>
    </main>

    <footer class="workspace-footer">Team.s · 人负责验证，协作留下记录。</footer>
  </div>
  </ElConfigProvider>
</template>
