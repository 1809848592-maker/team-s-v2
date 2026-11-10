<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api, dateText, errorText, projectPath, requestLabels, utcDate } from '../api/client'
import type { CollaborationRequest, Member, Module } from '../api/types'

const props = defineProps<{ projectId: string; members: Member[]; accountId: string }>()
const emit = defineEmits<{ changed: [] }>()
const modules = ref<Module[]>([]), requests = ref<CollaborationRequest[]>([])
const selectedId = ref(''), detail = ref<CollaborationRequest | null>(null), mineOnly = ref(false)
const loading = ref(false), detailLoading = ref(false), busy = ref(false)
const loadError = ref(''), detailError = ref(''), actionError = ref(''), notice = ref('')
const title = ref(''), content = ref(''), targetKind = ref('person'), targetId = ref(''), expectedBy = ref('')
const responseContent = ref(''), supplementContent = ref('')
let loadSequence = 0, detailSequence = 0
function name(id: string) { return props.members.find(member => member.user_id === id)?.username ?? id }
function moduleName(id: string) { return modules.value.find(module => module.id === id)?.name ?? `历史模块 ${id}` }
function targetLabel(item: CollaborationRequest) { return item.target_user_id ? name(item.target_user_id) : item.target_module_id ? moduleName(item.target_module_id) : '未指定' }
function mayAccept(item: CollaborationRequest) {
  return item.status === 'open' && (item.target_user_id === props.accountId || modules.value.some(module => module.id === item.target_module_id && module.assignee_ids.includes(props.accountId)))
}
function needsMe(item: CollaborationRequest) {
  return mayAccept(item) || item.status === 'accepted' && item.accepted_by === props.accountId || item.status === 'responded' && item.created_by === props.accountId
}
const visibleRequests = computed(() => mineOnly.value ? requests.value.filter(needsMe) : requests.value)
const canAccept = computed(() => !!detail.value && mayAccept(detail.value))
const canRespond = computed(() => !!detail.value && ['accepted', 'responded'].includes(detail.value.status) && detail.value.accepted_by === props.accountId)
const canResolve = computed(() => detail.value?.status === 'responded' && detail.value.created_by === props.accountId)
const canSupplement = computed(() => !!detail.value && detail.value.status !== 'resolved' && detail.value.created_by === props.accountId)
function selectVisible() {
  if (!visibleRequests.value.some(item => item.id === selectedId.value)) selectedId.value = visibleRequests.value[0]?.id ?? ''
}
async function load() {
  if (busy.value) return
  const sequence = ++loadSequence
  const path = projectPath(props.projectId)
  loading.value = true; loadError.value = ''
  try {
    const [nextModules, nextRequests] = await Promise.all([api<Module[]>(`${path}/modules`), api<CollaborationRequest[]>(`${path}/requests`)])
    if (sequence !== loadSequence) return
    modules.value = nextModules; requests.value = nextRequests
    const previousId = selectedId.value
    selectVisible()
    if (previousId && previousId === selectedId.value) await loadDetail()
  } catch (error) { if (sequence === loadSequence) loadError.value = errorText(error) }
  finally { if (sequence === loadSequence) loading.value = false }
}
async function loadDetail() {
  const sequence = ++detailSequence
  detail.value = null; detailError.value = ''
  if (!selectedId.value) { detailLoading.value = false; return }
  detailLoading.value = true
  try {
    const result = await api<CollaborationRequest>(`${projectPath(props.projectId)}/requests/${encodeURIComponent(selectedId.value)}`)
    if (sequence === detailSequence) detail.value = result
  } catch (error) { if (sequence === detailSequence) detailError.value = errorText(error) }
  finally { if (sequence === detailSequence) detailLoading.value = false }
}
async function create() {
  if (busy.value || loading.value || !title.value.trim() || !content.value.trim() || !targetId.value) return
  busy.value = true; actionError.value = ''; notice.value = ''
  try {
    const result = await api<CollaborationRequest>(`${projectPath(props.projectId)}/requests`, {
      title: title.value.trim(), content: content.value.trim(),
      target_user_id: targetKind.value === 'person' ? targetId.value : null,
      target_module_id: targetKind.value === 'module' ? targetId.value : null,
      expected_by: utcDate(expectedBy.value),
    })
    requests.value = [result, ...requests.value]; mineOnly.value = false; selectedId.value = result.id
    title.value = ''; content.value = ''; targetId.value = ''; expectedBy.value = ''
    notice.value = '协作需求已发起。'; emit('changed')
  } catch (error) { actionError.value = errorText(error) }
  finally { busy.value = false }
}
async function act(action: 'accept' | 'respond' | 'supplement' | 'resolve') {
  if (busy.value || loading.value || detailLoading.value || !detail.value) return
  if ((action === 'respond' && !responseContent.value.trim()) || (action === 'supplement' && !supplementContent.value.trim())) return
  busy.value = true; actionError.value = ''; notice.value = ''
  const payload = action === 'respond' ? { content: responseContent.value.trim() } : action === 'supplement' ? { content: supplementContent.value.trim() } : undefined
  try {
    const result = await api<CollaborationRequest>(`${projectPath(props.projectId)}/requests/${encodeURIComponent(detail.value.id)}/${action}`, payload, 'POST')
    detail.value = result; requests.value = requests.value.map(item => item.id === result.id ? result : item)
    if (action === 'respond') responseContent.value = ''
    if (action === 'supplement') supplementContent.value = ''
    notice.value = { accept: '已接手需求，可以在下方回应。', respond: '回应已发送，等待发起人确认。', supplement: '补充已加入原需求。', resolve: '已确认需求解决。' }[action]
    emit('changed')
  } catch (error) { actionError.value = errorText(error) }
  finally { busy.value = false }
}
watch(targetKind, () => { targetId.value = '' })
watch(mineOnly, selectVisible)
watch(selectedId, () => {
  responseContent.value = ''; supplementContent.value = ''; actionError.value = ''; void loadDetail()
})
watch(() => props.projectId, () => {
  requests.value = []; modules.value = []; selectedId.value = ''; detail.value = null
  title.value = ''; content.value = ''; targetId.value = ''; expectedBy.value = ''
  actionError.value = ''; notice.value = ''; void load()
}, { immediate: true })
</script>

<template>
  <section class="panel" aria-labelledby="request-title">
    <div class="panel-heading"><div><p class="eyebrow">接手 · 回应 · 确认</p><h2 id="request-title">协作需求</h2></div><button class="btn quiet" :disabled="loading || busy" @click="load()">刷新需求</button></div>
    <p v-if="loadError" class="error" role="alert">{{ loadError }} <button class="btn quiet" :disabled="loading || busy" @click="load()">重试读取</button></p>
    <p v-if="loading" class="muted" role="status">正在读取协作需求…</p>
    <p v-if="actionError" class="error" role="alert">{{ actionError }}</p><p v-if="notice" class="notice" role="status">{{ notice }}</p>
    <details class="create-request"><summary>发起协作需求</summary><form class="stack request-create-form" @submit.prevent="create">
      <div class="field"><label for="request-new-title">需求标题</label><input id="request-new-title" v-model="title" required maxlength="200" :disabled="busy || loading"></div>
      <div class="field"><label for="request-new-content">需求内容</label><textarea id="request-new-content" v-model="content" required maxlength="10000" rows="4" :disabled="busy || loading" placeholder="说明需要对方提供什么，以及用途" /></div>
      <div class="form-grid"><div class="field"><label for="request-target-kind">需求对象类型</label><select id="request-target-kind" v-model="targetKind" :disabled="busy || loading"><option value="person">成员</option><option value="module">模块</option></select></div><div class="field"><label for="request-target">{{ targetKind === 'person' ? '指定成员' : '指定模块' }}</label><select id="request-target" v-model="targetId" required :disabled="busy || loading"><option disabled value="">请选择</option><template v-if="targetKind === 'person'"><option v-for="member in members" :key="member.user_id" :value="member.user_id">{{ member.username }}</option></template><template v-else><option v-for="module in modules" :key="module.id" :value="module.id">{{ module.name }}</option></template></select></div><div class="field"><label for="request-expected">期望回应时间（选填）</label><input id="request-expected" v-model="expectedBy" type="datetime-local" :disabled="busy || loading"></div></div>
      <p v-if="targetKind === 'module' && !modules.length" class="muted">发布分工模块后，可向模块发起需求。</p>
      <button class="btn primary" :disabled="busy || loading || !title.trim() || !content.trim() || !targetId">{{ busy ? '正在处理…' : '发起需求' }}</button>
    </form></details>
    <div class="request-filter"><label class="check-label"><input v-model="mineOnly" type="checkbox" :disabled="busy || loading"> 只看待我处理</label><span class="muted">{{ visibleRequests.length }} 条需求</span></div>
    <p v-if="!visibleRequests.length && !loading && !loadError" class="empty">{{ mineOnly ? '当前没有需要你接手、回应或确认的需求。' : '还没有协作需求。需要他人提供输入时，可以在上方发起。' }}</p>
    <div v-if="visibleRequests.length || detail" class="request-layout">
      <nav class="request-list" aria-label="需求列表"><button v-for="item in visibleRequests" :key="item.id" type="button" class="request-choice" :class="{ selected: selectedId === item.id }" :aria-current="selectedId === item.id ? 'true' : undefined" :disabled="busy || loading" @click="selectedId = item.id"><span class="request-choice-heading"><strong>{{ item.title }}</strong><span class="tag">{{ requestLabels[item.status] }}</span></span><span class="muted">{{ name(item.created_by) }} → {{ targetLabel(item) }}</span><span class="request-date">{{ dateText(item.created_at) }}</span></button></nav>
      <div class="request-detail"><p v-if="detailLoading" class="muted" role="status">正在读取需求详情…</p><p v-if="detailError" class="error" role="alert">{{ detailError }} <button class="btn quiet" :disabled="busy || loading || detailLoading" @click="loadDetail()">重试详情</button></p>
        <template v-if="detail">
          <div class="toolbar"><h3>{{ detail.title }}</h3><span class="tag">{{ requestLabels[detail.status] }}</span></div>
          <p class="muted">{{ name(detail.created_by) }} 发起 · {{ dateText(detail.created_at) }}</p><p class="request-content pre-wrap">{{ detail.content }}</p>
          <dl class="request-meta"><dt>发给</dt><dd>{{ targetLabel(detail) }}</dd><dt>期望回应</dt><dd>{{ dateText(detail.expected_by) }}</dd><template v-if="detail.accepted_by"><dt>接手人</dt><dd>{{ name(detail.accepted_by) }} · {{ dateText(detail.accepted_at) }}</dd></template><template v-if="detail.responded_at"><dt>最近回应</dt><dd>{{ dateText(detail.responded_at) }}</dd></template><template v-if="detail.resolved_at"><dt>确认解决</dt><dd>{{ name(detail.created_by) }} · {{ dateText(detail.resolved_at) }}</dd></template></dl>
          <p v-if="detail.status === 'open'" class="state-note">等待指定成员或指定模块的负责人接手。</p><p v-else-if="detail.status === 'accepted'" class="state-note">已由 {{ name(detail.accepted_by!) }} 接手，等待回应。</p><p v-else-if="detail.status === 'responded'" class="state-note">已回应，等待 {{ name(detail.created_by) }} 确认解决。</p><p v-else class="state-note">发起人已确认解决，本条需求已结束。</p>
          <div class="toolbar request-actions"><button v-if="canAccept" class="btn primary" :disabled="busy || loading" @click="act('accept')">{{ busy ? '正在处理…' : '接手需求' }}</button><button v-if="canResolve" class="btn primary" :disabled="busy || loading" @click="act('resolve')">{{ busy ? '正在处理…' : '确认解决' }}</button></div>
          <section class="message-history" aria-labelledby="request-messages-title"><h4 id="request-messages-title">回应与补充</h4><p v-if="!detail.messages.length" class="muted">还没有回应或补充。</p><ol><li v-for="message in detail.messages" :key="message.id"><div class="toolbar"><strong>{{ message.source === 'agent' ? `${message.agent_name || '智能体'} 代表 ${name(message.author_id)}` : `${name(message.author_id)} · 本人` }}</strong><span class="message-kind">{{ message.kind === 'response' ? '回应' : '补充' }}</span><time :datetime="message.created_at">{{ dateText(message.created_at) }}</time></div><p class="pre-wrap">{{ message.content }}</p></li></ol></section>
          <form v-if="canRespond" class="stack response-form" @submit.prevent="act('respond')"><div class="field"><label for="request-response">回应内容</label><textarea id="request-response" v-model="responseContent" required maxlength="10000" rows="4" :disabled="busy || loading" /></div><button class="btn primary" :disabled="busy || loading || !responseContent.trim()">{{ busy ? '正在发送…' : '发送回应' }}</button></form>
          <form v-if="canSupplement" class="stack response-form" @submit.prevent="act('supplement')"><div class="field"><label for="request-supplement">补充需求</label><textarea id="request-supplement" v-model="supplementContent" required maxlength="10000" rows="3" :disabled="busy || loading" /></div><button class="btn" :disabled="busy || loading || !supplementContent.trim()">{{ busy ? '正在发送…' : '添加补充' }}</button></form>
        </template>
      </div>
    </div>
  </section>
</template>

<style scoped>
.create-request{margin:24px 0;border:1px solid var(--line);border-radius:8px;padding:18px}.create-request>summary{cursor:pointer;font-weight:600;color:var(--ink)}.request-create-form{margin-top:22px}.request-create-form>.btn,.response-form>.btn{justify-self:start}.request-filter{display:flex;justify-content:space-between;align-items:center;margin:26px 0 18px;font-size:13px}.check-label{display:flex;align-items:center;gap:8px}.check-label input{width:16px;height:16px;accent-color:var(--teal)}.request-layout{display:grid;grid-template-columns:minmax(240px,0.8fr) minmax(0,1.6fr);gap:26px}.request-list{display:flex;flex-direction:column;gap:10px;align-self:start}.request-choice{display:flex;flex-direction:column;gap:10px;text-align:left;padding:16px;border:1px solid var(--line);background:white;border-radius:7px;cursor:pointer;color:inherit;font:inherit;overflow-wrap:anywhere}.request-choice.selected{border-color:var(--teal);box-shadow:inset 3px 0 var(--teal);background:#f4faf9}.request-choice:disabled{cursor:wait}.request-choice-heading{display:flex;align-items:start;justify-content:space-between;gap:12px}.request-choice-heading strong{font-size:14px}.request-choice-heading .tag{font-size:11px;white-space:nowrap}.request-choice>.muted{font-size:12px}.request-date{font-size:11px;color:var(--muted)}.request-detail{min-width:0;border-left:1px solid var(--line);padding-left:26px}.request-detail h3{margin:0;overflow-wrap:anywhere}.request-content{margin:22px 0}.request-meta{display:grid;grid-template-columns:80px 1fr;gap:12px;font-size:13px}.request-meta dt{color:var(--muted)}.request-meta dd{margin:0;overflow-wrap:anywhere}.state-note{padding:14px 16px;background:var(--canvas);font-size:13px;color:var(--muted);border-radius:4px}.request-actions{margin:18px 0}.message-history{border-top:1px solid var(--line);margin-top:24px;padding-top:20px}.message-history h4{font-size:14px;margin:0 0 18px}.message-history ol{padding:0;list-style:none}.message-history li{padding:16px 0;border-bottom:1px solid var(--line)}.message-history li strong{font-size:13px}.message-history li time{margin-left:auto;font-size:11px;color:var(--muted)}.message-history li p{font-size:14px;margin:10px 0 0}.message-kind{font-size:11px;color:var(--teal)}.response-form{margin-top:24px}@media(max-width:900px){.request-layout{grid-template-columns:1fr}.request-detail{border-left:0;border-top:1px solid var(--line);padding:24px 0 0}.request-list{max-height:340px;overflow:auto}.request-choice-heading{align-items:center}}
</style>
