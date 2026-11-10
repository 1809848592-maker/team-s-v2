<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { api, dateText, errorText, projectPath } from '../api/client'
import { buildMcpSnippet, mcpClients } from '../api/mcpSnippets'
import type { AgentCreated, AgentInfo } from '../api/types'

const props = defineProps<{ projectId: string; accountId: string }>()
const emit = defineEmits<{ changed: [] }>()
const agents = ref<AgentInfo[]>([]), loading = ref(false), registering = ref(false), revokingId = ref('')
const name = ref(''), clientType = ref(''), customClient = ref(''), revokeChoice = ref('')
const loadError = ref(''), actionError = ref(''), notice = ref(''), copyNotice = ref(''), copyError = ref('')
const token = ref(''), tokenAgent = ref<AgentInfo | null>(null), copying = ref('')
const tokenInput = ref<HTMLTextAreaElement | null>(null)
const selectedClientType = computed(() => clientType.value === 'other' ? customClient.value.trim() : clientType.value)
const ownAgents = computed(() => agents.value.filter(item => item.owner_id === props.accountId && item.project_id === props.projectId))
const busy = computed(() => registering.value || !!revokingId.value)
const snippetOptions = computed(() => ({ endpoint: new URL('/mcp', window.location.origin).href, serverName: `teams-${props.projectId}`, tokenEnv: 'TEAMS_AGENT_TOKEN' }))
const snippets = computed(() => mcpClients.map(client => ({ client, ...buildMcpSnippet(client, snippetOptions.value) })))
let mounted = true, contextSequence = 0, loadSequence = 0, copySequence = 0

function context() { return { sequence: contextSequence, projectId: props.projectId, accountId: props.accountId } }
function current(value: ReturnType<typeof context>) { return mounted && value.sequence === contextSequence && value.projectId === props.projectId && value.accountId === props.accountId }
function clearToken() {
  if (tokenInput.value) tokenInput.value.value = ''
  token.value = ''; tokenAgent.value = null; copying.value = ''; copyNotice.value = ''; copyError.value = ''; ++copySequence
}
function pageLeaving() {
  ++contextSequence; ++loadSequence; clearToken()
  registering.value = false; revokingId.value = ''; loading.value = false
}
function pageReturning(event: PageTransitionEvent) { if (event.persisted) void load() }

async function load() {
  if (busy.value) return
  clearToken()
  const owner = context(), sequence = ++loadSequence
  loading.value = true; loadError.value = ''; actionError.value = ''; notice.value = ''; revokeChoice.value = ''
  agents.value = []
  try {
    const result = await api<AgentInfo[]>(`${projectPath(owner.projectId)}/agents`)
    if (current(owner) && sequence === loadSequence) agents.value = result.filter(item => item.owner_id === owner.accountId && item.project_id === owner.projectId)
  } catch (error) { if (current(owner) && sequence === loadSequence) loadError.value = errorText(error) }
  finally { if (current(owner) && sequence === loadSequence) loading.value = false }
}

async function register() {
  if (loading.value || busy.value || token.value || !name.value.trim() || !selectedClientType.value) return
  const owner = context()
  registering.value = true; actionError.value = ''; notice.value = ''; copyNotice.value = ''; copyError.value = ''; revokeChoice.value = ''
  try {
    const result = await api<AgentCreated>(`${projectPath(owner.projectId)}/agents`, { name: name.value.trim(), client_type: selectedClientType.value })
    if (!current(owner)) return
    agents.value = [result.agent, ...agents.value.filter(item => item.id !== result.agent.id)]
    tokenAgent.value = result.agent; token.value = result.token
    name.value = ''; clientType.value = ''; customClient.value = ''
    notice.value = '智能体已登记。请现在保存令牌，关闭后无法再次查看。'
    emit('changed')
  } catch (error) { if (current(owner)) actionError.value = errorText(error) }
  finally { if (current(owner)) registering.value = false }
}

async function revoke(agent: AgentInfo) {
  if (loading.value || busy.value || revokeChoice.value !== agent.id || agent.revoked_at || agent.owner_id !== props.accountId) return
  const owner = context(), agentId = agent.id
  revokingId.value = agentId; actionError.value = ''; notice.value = ''
  try {
    const result = await api<AgentInfo>(`${projectPath(owner.projectId)}/agents/${encodeURIComponent(agentId)}/revoke`, undefined, 'POST')
    if (!current(owner)) return
    agents.value = agents.value.map(item => item.id === agentId ? result : item)
    if (tokenAgent.value?.id === agentId) clearToken()
    revokeChoice.value = ''; notice.value = `${result.name} 的令牌已吊销，后续调用会被拒绝。`
    emit('changed')
  } catch (error) { if (current(owner)) actionError.value = errorText(error) }
  finally { if (current(owner)) revokingId.value = '' }
}

function selectText(input: HTMLTextAreaElement | null) { input?.focus(); input?.select() }
async function copy(value: string, key: string, inputId: string, label: string) {
  if (copying.value || !value) return
  const owner = context(), sequence = ++copySequence
  copying.value = key; copyNotice.value = ''; copyError.value = ''
  try {
    if (!navigator.clipboard?.writeText) throw new Error('clipboard unavailable')
    await navigator.clipboard.writeText(value)
    if (current(owner) && sequence === copySequence) copyNotice.value = `${label}已复制。`
  } catch {
    if (!current(owner) || sequence !== copySequence) return
    copyError.value = '浏览器未允许自动复制。已选中文本，请使用键盘复制。'
    await nextTick()
    if (current(owner) && sequence === copySequence) selectText(document.getElementById(inputId) as HTMLTextAreaElement | null)
  } finally { if (current(owner) && sequence === copySequence) copying.value = '' }
}

watch(clientType, () => { customClient.value = '' })
watch(() => [props.projectId, props.accountId], () => {
  ++contextSequence; ++loadSequence
  clearToken(); agents.value = []; name.value = ''; clientType.value = ''; customClient.value = ''; revokeChoice.value = ''
  registering.value = false; revokingId.value = ''; actionError.value = ''; notice.value = ''
  void load()
}, { immediate: true })
window.addEventListener('pagehide', pageLeaving)
window.addEventListener('beforeunload', pageLeaving)
window.addEventListener('pageshow', pageReturning)
onBeforeUnmount(() => {
  mounted = false; ++contextSequence; ++loadSequence; clearToken()
  window.removeEventListener('pagehide', pageLeaving)
  window.removeEventListener('beforeunload', pageLeaving)
  window.removeEventListener('pageshow', pageReturning)
})
</script>

<template>
  <section class="panel agent-panel" aria-labelledby="agent-title">
    <div class="panel-heading"><div><p class="eyebrow">代表你 · 在当前项目协作</p><h2 id="agent-title">我的智能体</h2></div><button class="btn quiet" :disabled="loading || busy" @click="load">刷新智能体</button></div>
    <p class="muted agent-intro">智能体挂在你名下，权限限于当前项目和你负责的任务。它上传的交付稿需要你在网页确认提交。</p>
    <p v-if="loadError" class="error" role="alert">{{ loadError }} <button class="btn quiet" :disabled="loading || busy" @click="load">重试读取智能体</button></p>
    <p v-if="loading" class="muted" role="status">正在读取你的智能体…</p>
    <p v-if="actionError" class="error" role="alert">{{ actionError }}</p><p v-if="notice" class="notice" role="status">{{ notice }}</p>

    <section v-if="token && tokenAgent" class="token-card" aria-labelledby="agent-token-title">
      <div class="panel-heading"><div><p class="eyebrow">仅本次显示</p><h3 id="agent-token-title">保存 {{ tokenAgent.name }} 的令牌</h3></div><span class="tag">一次性显示</span></div>
      <p class="muted">请复制到客户端所需的位置。刷新、离开本页或切换项目后会清除；遗失后请吊销该智能体并重新登记。</p>
      <div class="field"><label for="agent-token">令牌（仅本次显示）</label><textarea id="agent-token" ref="tokenInput" :value="token" readonly rows="2" autocomplete="off" autocapitalize="off" :spellcheck="false" @focus="selectText(tokenInput)" /></div>
      <div class="toolbar token-actions"><button class="btn primary" :disabled="!!copying" @click="copy(token, 'token', 'agent-token', '令牌')">复制令牌</button><button class="btn" @click="selectText(tokenInput)">选中令牌</button><button class="btn quiet" :disabled="busy" @click="clearToken">已保存，关闭令牌</button></div>
    </section>
    <p v-if="copyNotice" class="notice" role="status">{{ copyNotice }}</p><p v-if="copyError" class="error" role="alert">{{ copyError }}</p>

    <div class="agent-management">
      <form class="record stack agent-register" @submit.prevent="register">
        <h3>登记智能体</h3>
        <div class="field"><label for="agent-name">智能体名称</label><input id="agent-name" v-model="name" required maxlength="120" :disabled="loading || busy || !!token" placeholder="例如：文献整理助手" autocomplete="off"></div>
        <div class="field"><label for="agent-client">客户端类型</label><select id="agent-client" v-model="clientType" required :disabled="loading || busy || !!token"><option disabled value="">请选择</option><option value="Claude Code">Claude Code</option><option value="Codex">Codex</option><option value="Cursor">Cursor</option><option value="other">其他</option></select></div>
        <div v-if="clientType === 'other'" class="field"><label for="agent-custom-client">其他客户端名称</label><input id="agent-custom-client" v-model="customClient" required maxlength="80" :disabled="loading || busy || !!token" autocomplete="off"></div>
        <p class="muted">每次登记生成一个独立令牌。{{ token ? '请先保存并关闭上方令牌，再登记下一个智能体。' : '可以为同一项目登记多个智能体。' }}</p>
        <button class="btn primary" :disabled="loading || busy || !!token || !name.trim() || !selectedClientType">{{ registering ? '正在登记…' : '登记智能体' }}</button>
      </form>
      <section class="agent-list" aria-labelledby="agent-list-title"><div class="toolbar agent-list-heading"><h3 id="agent-list-title">本项目中我的登记</h3><span class="muted">{{ ownAgents.length }} 个</span></div>
        <p v-if="!ownAgents.length && !loading && !loadError" class="empty">还没有登记智能体。先填写名称和客户端类型，获取接入令牌。</p>
        <div class="stack"><article v-for="agent in ownAgents" :key="agent.id" :data-agent-id="agent.id" class="record agent-record">
          <div class="toolbar"><h4>{{ agent.name }}</h4><span class="tag" :class="{ submitted: !agent.revoked_at }">{{ agent.revoked_at ? '已吊销' : '有效' }}</span></div>
          <p class="muted">{{ agent.client_type }} · 登记于 {{ dateText(agent.created_at) }}</p>
          <p v-if="agent.revoked_at" class="muted">吊销于 {{ dateText(agent.revoked_at) }}</p>
          <template v-else><div v-if="revokeChoice === agent.id" class="revoke-confirm"><p>吊销后，该令牌会立即失效。需要再次接入时，请重新登记智能体。</p><div class="toolbar"><button class="btn danger" :disabled="loading || busy" @click="revoke(agent)">{{ revokingId === agent.id ? '正在吊销…' : '确认吊销' }}</button><button class="btn quiet" :disabled="loading || busy" @click="revokeChoice = ''; actionError = ''">取消吊销</button></div></div><button v-else class="btn danger" :disabled="loading || busy" @click="revokeChoice = agent.id; actionError = ''">吊销令牌</button></template>
        </article></div>
      </section>
    </div>

    <section class="connection-config" aria-labelledby="mcp-config-title">
      <div class="panel-heading"><div><p class="eyebrow">MCP 接入</p><h3 id="mcp-config-title">选择你的客户端配置</h3></div></div>
      <p class="muted">将刚保存的令牌设置为环境变量 <code>TEAMS_AGENT_TOKEN</code>，再复制对应客户端配置。配置中的项目名称用于区分连接，令牌决定实际权限。</p>
      <p class="muted">服务地址：<code>{{ snippetOptions.endpoint }}</code></p>
      <div class="stack"><details v-for="snippet in snippets" :key="snippet.client" class="snippet-card" open><summary>{{ snippet.title }} 配置</summary><p class="muted">{{ snippet.instructions }}</p><p class="config-file">{{ snippet.fileName }}</p><div class="field"><label :for="`mcp-snippet-${snippet.client}`">{{ snippet.title }} 配置片段</label><textarea :id="`mcp-snippet-${snippet.client}`" :value="snippet.content" readonly :rows="Math.min(15, Math.max(4, snippet.content.split('\n').length))" :spellcheck="false" autocapitalize="off" @focus="selectText($event.target as HTMLTextAreaElement)" /></div><div class="toolbar snippet-actions"><button class="btn" :disabled="!!copying" @click="copy(snippet.content, snippet.client, `mcp-snippet-${snippet.client}`, `${snippet.title} 配置`)">复制 {{ snippet.title }} 配置</button></div></details></div>
    </section>
  </section>
</template>

<style scoped>
.agent-intro{margin:-8px 0 24px;max-width:760px}.agent-management{display:grid;grid-template-columns:minmax(250px,.8fr) minmax(0,1.2fr);gap:28px;margin-top:28px;align-items:start}.agent-register h3{margin:0}.agent-register>.btn{justify-self:start}.agent-register .muted{margin:0}.agent-list-heading{justify-content:space-between;margin-bottom:18px}.agent-list-heading h3{margin:0}.agent-record h4{font-size:15px;margin:0;overflow-wrap:anywhere}.agent-record>.toolbar{justify-content:space-between}.agent-record .muted{font-size:12px}.token-card{border:1px solid #b6d8cd;border-left:3px solid var(--teal);background:#f4faf8;border-radius:6px;padding:24px;margin:24px 0}.token-card .panel-heading{margin-bottom:10px}.token-card h3{margin:0}.token-card .eyebrow{margin-bottom:6px}.token-actions,.snippet-actions{margin-top:16px}.token-card textarea,.snippet-card textarea{font:12px/1.7 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;overflow-wrap:anywhere;resize:vertical}.revoke-confirm{border-top:1px solid var(--line);padding-top:12px}.revoke-confirm p{font-size:13px;line-height:1.7;margin:0 0 14px}.connection-config{margin-top:32px;padding-top:28px;border-top:1px solid var(--line)}.connection-config .panel-heading{margin-bottom:10px}.connection-config h3{margin:0}.connection-config code{font:12px ui-monospace,monospace;overflow-wrap:anywhere}.snippet-card{border:1px solid var(--line);border-radius:6px;padding:20px;min-width:0}.snippet-card>summary{cursor:pointer;font-size:15px;font-weight:600}.snippet-card .config-file{font:12px ui-monospace,monospace;color:var(--muted);overflow-wrap:anywhere}.snippet-card .field{margin-top:18px}@media(max-width:1000px){.agent-management{grid-template-columns:1fr}.token-card{padding:20px}.snippet-card{padding:16px}}
</style>
