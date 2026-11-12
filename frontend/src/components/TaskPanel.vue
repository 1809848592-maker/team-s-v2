<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { ApiError, api, dateText, errorText, projectPath, sizeText, stageLabels } from '../api/client'
import type { Delivery, DraftView, Material, Member, Module, ModuleProgress, Progress } from '../api/types'
import DeliveryReviewStatus from './DeliveryReviewStatus.vue'
import DeliveryVersionBinding from './DeliveryVersionBinding.vue'
import { useReviewPolling } from '../composables/useReviewPolling'

const props = defineProps<{ projectId: string; members: Member[]; accountId: string; initialModuleId?: string }>()
const emit = defineEmits<{ changed: [] }>()
const modules = ref<Module[]>([]), progress = ref<ModuleProgress[]>([])
const deliveries = ref<Delivery[]>([]), materials = ref<Material[]>([]), reports = ref<Progress[]>([])
const drafts = ref<DraftView[]>([]), selectedDraftId = ref(''), confirmingDraftId = ref('')
const draftAi = ref(''), draftAiNote = ref('')
const selectedId = ref(''), loading = ref(false), historyLoading = ref(false)
const loadError = ref(''), historyError = ref(''), actionError = ref(''), notice = ref('')
const reportBusy = ref(false), deliveryBusy = ref(false), materialBusy = ref(false)
const requestingResubmissionId = ref('')
const stage = ref('planning'), summary = ref(''), ai = ref(''), aiNote = ref(''), handoff = ref('')
const deliveryFile = ref<File | null>(null), materialFile = ref<File | null>(null)
const deliveryInput = ref<HTMLInputElement | null>(null), materialInput = ref<HTMLInputElement | null>(null)
let loadSequence = 0, historySequence = 0, contextSequence = 0, mounted = true
let initialSelectionApplied = false
const currentModule = computed(() => modules.value.find(item => item.id === selectedId.value))
const currentProgress = computed(() => progress.value.find(item => item.module_id === selectedId.value))
const currentDeliveries = computed(() => deliveries.value.filter(item => item.module_id === selectedId.value))
const archivedDeliveries = computed(() => deliveries.value.filter(item => !modules.value.some(module => module.id === item.module_id)))
const ownDrafts = computed(() => drafts.value.filter(item => item.owner_id === props.accountId))
const pendingDrafts = computed(() => ownDrafts.value.filter(item => item.status === 'pending_confirmation'))
const confirmedDrafts = computed(() => ownDrafts.value.filter(item => item.status === 'confirmed'))
const canReport = computed(() => currentModule.value?.assignee_ids.includes(props.accountId) ?? false)
const busy = computed(() => reportBusy.value || deliveryBusy.value || materialBusy.value || !!confirmingDraftId.value || !!requestingResubmissionId.value)
const { error: reviewError, refreshing: reviewRefreshing, retry: retryReview } = useReviewPolling({
  contextKey: () => `${props.projectId}:${props.accountId}:${selectedId.value}`,
  projectId: () => props.projectId,
  enabled: () => !loading.value && !busy.value && !loadError.value,
  deliveries,
})
const stages = ['not_started', 'planning', 'writing', 'checking']
const aiLabels: Record<string, string> = { none: '无 AI 参与', assisted: 'AI 辅助', primary: '主要由 AI 生成' }
const formatLabels: Record<string, string> = { docx: 'Word · DOCX', pptx: 'PowerPoint · PPTX', pdf: 'PDF', md: 'Markdown', other: '其他' }
function name(id: string) { return props.members.find(member => member.user_id === id)?.username ?? id }
function moduleName(id: string) { return modules.value.find(module => module.id === id)?.name ?? id }
function progressSource(item: Progress) { return item.source === 'agent' ? `${item.agent_name || '智能体'} 代表 ${name(item.user_id)}` : `${name(item.user_id)} · 本人` }
function mayConfirm(draft: DraftView) { return draft.owner_id === props.accountId && modules.value.some(module => module.id === draft.module_id && module.assignee_ids.includes(props.accountId)) }
function draftDelivery(draft: DraftView) { return deliveries.value.find(item => item.id === draft.delivery_id) }
function context() { return { sequence: contextSequence, projectId: props.projectId, accountId: props.accountId } }
function current(value: ReturnType<typeof context>) { return mounted && value.sequence === contextSequence && value.projectId === props.projectId && value.accountId === props.accountId }
function fileFrom(event: Event) { return (event.target as HTMLInputElement).files?.[0] ?? null }
async function load() {
  const owner = context()
  const sequence = ++loadSequence
  const path = projectPath(owner.projectId)
  loading.value = true; loadError.value = ''
  try {
    const [nextModules, nextProgress, nextDeliveries, nextMaterials, nextDrafts] = await Promise.all([
      api<Module[]>(`${path}/modules`), api<ModuleProgress[]>(`${path}/progress`),
      api<Delivery[]>(`${path}/deliveries`), api<Material[]>(`${path}/materials`),
      api<DraftView[]>(`${path}/drafts`),
    ])
    if (!current(owner) || sequence !== loadSequence) return
    const previousSelectedId = selectedId.value
    modules.value = nextModules; progress.value = nextProgress
    deliveries.value = nextDeliveries; materials.value = nextMaterials
    drafts.value = nextDrafts.filter(item => item.owner_id === owner.accountId)
    if (!drafts.value.some(item => item.id === selectedDraftId.value && item.status === 'pending_confirmation')) selectedDraftId.value = ''
    if (!initialSelectionApplied) {
      if (props.initialModuleId && nextModules.some(item => item.id === props.initialModuleId)) selectedId.value = props.initialModuleId
      initialSelectionApplied = true
    }
    if (!nextModules.some(item => item.id === selectedId.value)) selectedId.value = nextModules[0]?.id ?? ''
    if (selectedId.value && selectedId.value === previousSelectedId) await loadHistory()
  } catch (error) { if (current(owner) && sequence === loadSequence) loadError.value = errorText(error) }
  finally { if (current(owner) && sequence === loadSequence) loading.value = false }
}
async function loadHistory() {
  const owner = context()
  const sequence = ++historySequence
  reports.value = []; historyError.value = ''
  if (!selectedId.value) { historyLoading.value = false; return }
  const path = `${projectPath(props.projectId)}/modules/${encodeURIComponent(selectedId.value)}/progress`
  historyLoading.value = true
  try {
    const result = await api<Progress[]>(path)
    if (current(owner) && sequence === historySequence) reports.value = result
  } catch (error) { if (current(owner) && sequence === historySequence) historyError.value = errorText(error) }
  finally { if (current(owner) && sequence === historySequence) historyLoading.value = false }
}
async function report() {
  if (!canReport.value || loading.value || busy.value || !summary.value.trim()) return
  const owner = context()
  reportBusy.value = true; actionError.value = ''; notice.value = ''
  try {
    await api<Progress>(`${projectPath(props.projectId)}/modules/${encodeURIComponent(selectedId.value)}/progress`, { stage: stage.value, summary: summary.value.trim() })
    if (!current(owner)) return
    summary.value = ''; notice.value = '进度已记录。'
    await load(); if (current(owner)) emit('changed')
  } catch (error) { if (current(owner)) actionError.value = errorText(error) }
  finally { if (current(owner)) reportBusy.value = false }
}
async function submitDelivery() {
  if (!canReport.value || loading.value || busy.value || !deliveryFile.value || !ai.value) return
  const owner = context()
  deliveryBusy.value = true; actionError.value = ''; notice.value = ''
  const body = new FormData()
  body.append('file', deliveryFile.value); body.append('ai_participation', ai.value)
  body.append('ai_note', aiNote.value.trim()); body.append('handoff_note', handoff.value.trim())
  try {
    await api<Delivery>(`${projectPath(props.projectId)}/modules/${encodeURIComponent(selectedId.value)}/deliveries`, body)
    if (!current(owner)) return
    deliveryFile.value = null; ai.value = ''; aiNote.value = ''; handoff.value = ''
    if (deliveryInput.value) deliveryInput.value.value = ''
    notice.value = '正式交付已提交，新版本已保留。'
    await load(); if (current(owner)) emit('changed')
  } catch (error) { if (current(owner)) actionError.value = errorText(error) }
  finally { if (current(owner)) deliveryBusy.value = false }
}
async function confirmDraft(draft: DraftView) {
  if (busy.value || loading.value || selectedDraftId.value !== draft.id || !draftAi.value || draft.status !== 'pending_confirmation' || !mayConfirm(draft)) return
  const owner = context(), draftId = draft.id
  const body = { ai_participation: draftAi.value, ai_note: draftAiNote.value.trim() }
  confirmingDraftId.value = draftId; actionError.value = ''; notice.value = ''
  try {
    const delivery = await api<Delivery>(`${projectPath(owner.projectId)}/drafts/${encodeURIComponent(draftId)}/confirm`, body)
    if (!current(owner)) return
    deliveries.value = [delivery, ...deliveries.value.filter(item => item.id !== delivery.id)]
    drafts.value = drafts.value.map(item => item.id === draftId ? { ...item, status: 'confirmed', delivery_id: delivery.id, confirmed_at: delivery.submitted_at } : item)
    progress.value = progress.value.map(item => item.module_id === delivery.module_id ? { ...item, stage: 'submitted', delivery_id: delivery.id } : item)
    selectedDraftId.value = ''; selectedId.value = delivery.module_id
    await nextTick()
    if (!current(owner)) return
    notice.value = '交付稿已确认，正式版本已提交。'
    await load(); if (current(owner)) emit('changed')
  } catch (error) {
    if (!current(owner)) return
    actionError.value = errorText(error)
    if (error instanceof ApiError && error.status === 409) { await load(); if (current(owner)) emit('changed') }
  } finally { if (current(owner)) confirmingDraftId.value = '' }
}
async function requestResubmission(delivery: Delivery) {
  const binding = delivery.version_binding
  if (busy.value || loading.value || !binding?.can_request_resubmission) return
  const owner = context()
  requestingResubmissionId.value = delivery.id; actionError.value = ''; notice.value = ''
  try {
    await api<Delivery>(`${projectPath(owner.projectId)}/deliveries/${encodeURIComponent(delivery.id)}/resubmission-requests`, {
      expected_plan_version_id: binding.latest_plan_version_id,
      expected_spec_version_id: binding.latest_spec_version_id,
    })
    if (!current(owner)) return
    notice.value = '重新提交要求已记录，提交人会收到站内通知。'
    await load(); if (current(owner)) emit('changed')
  } catch (error) {
    if (!current(owner)) return
    actionError.value = error instanceof ApiError && error.status === 409
      ? '方案、规范或交付状态已有更新，请核对刷新后的版本再操作。' : errorText(error)
    if (error instanceof ApiError && error.status === 409) { await load(); if (current(owner)) emit('changed') }
  } finally { if (current(owner)) requestingResubmissionId.value = '' }
}
async function uploadMaterial() {
  if (loading.value || busy.value || !materialFile.value) return
  const owner = context()
  materialBusy.value = true; actionError.value = ''; notice.value = ''
  const body = new FormData(); body.append('file', materialFile.value)
  try {
    await api<Material>(`${projectPath(props.projectId)}/materials`, body)
    if (!current(owner)) return
    materialFile.value = null
    if (materialInput.value) materialInput.value.value = ''
    notice.value = '共享材料已上传。'
    await load(); if (current(owner)) emit('changed')
  } catch (error) { if (current(owner)) actionError.value = errorText(error) }
  finally { if (current(owner)) materialBusy.value = false }
}
watch(selectedDraftId, () => { draftAi.value = ''; draftAiNote.value = ''; actionError.value = '' })
watch(selectedId, () => {
  summary.value = ''; stage.value = 'planning'; ai.value = ''; aiNote.value = ''; handoff.value = ''
  deliveryFile.value = null; if (deliveryInput.value) deliveryInput.value.value = ''
  actionError.value = ''; notice.value = ''; void loadHistory()
  selectedDraftId.value = ''
})
watch(() => [props.projectId, props.accountId], () => {
  ++contextSequence; ++loadSequence; ++historySequence
  initialSelectionApplied = false
  modules.value = []; progress.value = []; deliveries.value = []; materials.value = []; drafts.value = []
  selectedId.value = ''; reports.value = []; materialFile.value = null
  selectedDraftId.value = ''; draftAi.value = ''; draftAiNote.value = ''; confirmingDraftId.value = ''
  reportBusy.value = false; deliveryBusy.value = false; materialBusy.value = false
  requestingResubmissionId.value = ''
  actionError.value = ''; notice.value = ''; historyError.value = ''
  if (materialInput.value) materialInput.value.value = ''
  void load()
}, { immediate: true })
onBeforeUnmount(() => { mounted = false; ++contextSequence; ++loadSequence; ++historySequence })
</script>

<template>
  <section class="panel" aria-labelledby="task-title">
    <div class="panel-heading"><div><p class="eyebrow">分工与交接</p><h2 id="task-title">任务与交付</h2></div><button class="btn quiet" :disabled="loading || busy" @click="load()">刷新任务</button></div>
    <p v-if="loadError" class="error" role="alert">{{ loadError }} <button class="btn quiet" :disabled="loading || busy" @click="load()">重试读取</button></p>
    <p v-if="loading" class="muted" role="status">正在读取任务与文件…</p>
    <p v-if="actionError" class="error" role="alert">{{ actionError }}</p>
    <p v-if="notice" class="notice" role="status">{{ notice }}</p>
    <p v-if="reviewError" class="error" role="alert">审核进度读取失败：{{ reviewError }} <button class="btn quiet" :disabled="reviewRefreshing || loading || busy" @click="retryReview">重试读取审核进度</button></p>
    <div v-if="modules.length" class="field module-picker"><label for="task-module">当前任务</label><select id="task-module" v-model="selectedId" :disabled="busy || loading"><option v-for="module in modules" :key="module.id" :value="module.id">{{ module.name }}</option></select></div>
    <p v-else-if="!loading && !loadError" class="empty">尚未发布分工模块。负责人发布方案后，任务会显示在这里。</p>
    <section class="subsection private-drafts" aria-labelledby="draft-title">
      <div class="panel-heading"><div><h3 id="draft-title">我的智能体交付稿</h3><p class="muted">仅你可见。确认时请填写 AI 参与声明，原稿及交接说明会保留到正式版本。</p></div><span v-if="pendingDrafts.length" class="tag">{{ pendingDrafts.length }} 份待本人确认</span></div>
      <p v-if="!ownDrafts.length && !loading && !loadError" class="empty">还没有智能体交付稿。</p>
      <div class="stack"><article v-for="draft in pendingDrafts" :key="draft.id" class="record draft-record" :data-draft-id="draft.id">
        <div class="toolbar"><a :href="draft.download_url" download>{{ draft.filename }}</a><span class="tag">待本人确认</span></div>
        <p class="muted">{{ draft.agent_name }} 代表你上传 · {{ moduleName(draft.module_id) }} · {{ dateText(draft.created_at) }} · {{ sizeText(draft.size) }}</p>
        <p v-if="draft.handoff_note" class="pre-wrap">交接说明：{{ draft.handoff_note }}</p>
        <p v-else class="muted">未附交接说明。</p>
        <p v-if="!mayConfirm(draft)" class="muted">{{ modules.some(module => module.id === draft.module_id) ? '你已不再负责该模块，当前不能确认提交。原稿仍可下载。' : '该模块已移除，保留原稿供你查阅。' }}</p>
        <template v-else>
          <button v-if="selectedDraftId !== draft.id" class="btn" :disabled="busy || loading" @click="selectedDraftId = draft.id">填写声明并确认</button>
          <form v-else class="stack draft-confirm-form" @submit.prevent="confirmDraft(draft)">
            <div class="field"><label :for="`draft-ai-${draft.id}`">AI 参与声明</label><select :id="`draft-ai-${draft.id}`" v-model="draftAi" required :disabled="busy || loading"><option disabled value="">请选择</option><option v-for="(label, value) in aiLabels" :key="value" :value="value">{{ label }}</option></select></div>
            <div class="field"><label :for="`draft-ai-note-${draft.id}`">AI 参与说明（选填）</label><textarea :id="`draft-ai-note-${draft.id}`" v-model="draftAiNote" maxlength="2000" rows="2" :disabled="busy || loading" /></div>
            <p class="muted">确认后会绑定当前最新方案和规范，作为正式交付提交。</p>
            <div class="toolbar"><button class="btn primary" :data-confirm-draft="draft.id" :disabled="busy || loading || !draftAi">{{ confirmingDraftId === draft.id ? '正在确认…' : '确认提交' }}</button><button type="button" class="btn quiet" :disabled="busy || loading" @click="selectedDraftId = ''">取消</button></div>
          </form>
        </template>
      </article></div>
      <details v-if="confirmedDrafts.length" class="confirmed-drafts"><summary>已确认的交付稿（{{ confirmedDrafts.length }}）</summary><article v-for="draft in confirmedDrafts" :key="draft.id" class="record draft-record" :data-draft-id="draft.id"><div class="toolbar"><a :href="draft.download_url" download>{{ draft.filename }}（原稿）</a><span class="tag">已确认提交</span></div><p class="muted">{{ draft.agent_name }} 代表你上传 · {{ moduleName(draft.module_id) }} · 确认于 {{ dateText(draft.confirmed_at) }}</p><p v-if="draft.handoff_note" class="pre-wrap">交接说明：{{ draft.handoff_note }}</p><a v-if="draftDelivery(draft)" :href="draftDelivery(draft)!.download_url" download>下载正式交付 v{{ draftDelivery(draft)!.version }} · 已提交</a><DeliveryReviewStatus :status="draftDelivery(draft)?.review_status" :progress="draftDelivery(draft)?.review_progress" /></article></details>
    </section>
    <template v-if="currentModule">
      <div class="task-context">
        <div class="toolbar"><h3>{{ currentModule.name }}</h3><span class="tag">{{ stageLabels[currentProgress?.stage ?? 'not_started'] }}</span></div>
        <p class="muted">负责人：{{ currentModule.assignee_ids.map(name).join('、') }} · 截止：{{ dateText(currentModule.deadline) }}</p>
        <dl class="context-grid"><dt>模块目标</dt><dd>{{ currentModule.goal }}</dd><dt>核心论点</dt><dd>{{ currentModule.core_argument || '未填写' }}</dd><dt>包含内容</dt><dd>{{ currentModule.scope_in || '未填写' }}</dd><dt>不包含内容</dt><dd>{{ currentModule.scope_out || '未填写' }}</dd><dt>上游输入</dt><dd><ul v-if="currentModule.upstream.length" class="dependency-list"><li v-for="upstream in currentModule.upstream" :key="upstream.module_id"><strong>{{ moduleName(upstream.module_id) }}</strong>：{{ upstream.needs }}</li></ul><span v-else>无上游依赖</span></dd><dt>下游交接</dt><dd>{{ currentModule.downstream_handoff || '未填写' }}</dd><dt>交付形式</dt><dd>{{ formatLabels[currentModule.delivery_format] }}</dd></dl>
        <div class="latest-report"><h4>最后一次进度记录</h4><template v-if="currentProgress?.last_report"><p class="pre-wrap">{{ currentProgress.last_report.summary }}</p><p class="muted">{{ progressSource(currentProgress.last_report) }} · {{ stageLabels[currentProgress.last_report.stage] }} · {{ dateText(currentProgress.last_report.created_at) }}</p></template><p v-else class="muted">还没有进度回报。</p></div>
      </div>
      <div v-if="canReport" class="task-forms">
        <form class="record stack" @submit.prevent="report">
          <h3>回报进度</h3><div class="field"><label for="progress-stage">当前阶段</label><select id="progress-stage" v-model="stage" :disabled="busy || loading"><option v-for="value in stages" :key="value" :value="value">{{ stageLabels[value] }}</option></select></div>
          <div class="field"><label for="progress-summary">一句摘要</label><textarea id="progress-summary" v-model="summary" required maxlength="1000" rows="3" :disabled="busy || loading" placeholder="说明已推进的内容和当前卡点" /></div>
          <p v-if="currentProgress?.stage === 'submitted'" class="muted">交付已经提交；后续回报会保留为记录，任务阶段仍显示“已提交”。</p>
          <button class="btn primary" :disabled="busy || loading || !summary.trim()">{{ reportBusy ? '正在记录…' : '记录进度' }}</button>
        </form>
        <form class="record stack" @submit.prevent="submitDelivery">
          <h3>提交正式交付</h3><div class="field"><label for="delivery-file">交付文件</label><input id="delivery-file" ref="deliveryInput" type="file" required :disabled="busy || loading" @change="deliveryFile = fileFrom($event)"></div>
          <div class="field"><label for="delivery-ai">AI 参与声明</label><select id="delivery-ai" v-model="ai" required :disabled="busy || loading"><option disabled value="">请选择</option><option v-for="(label, value) in aiLabels" :key="value" :value="value">{{ label }}</option></select></div>
          <div class="field"><label for="delivery-ai-note">AI 参与说明（选填）</label><textarea id="delivery-ai-note" v-model="aiNote" maxlength="2000" rows="2" :disabled="busy || loading" /></div>
          <div class="field"><label for="delivery-handoff">交接说明（选填）</label><textarea id="delivery-handoff" v-model="handoff" maxlength="10000" rows="3" :disabled="busy || loading" /></div>
          <p class="muted">本次提交会绑定当前最新的方案和规范，并保留此前版本。</p>
          <button class="btn primary" :disabled="busy || loading || !deliveryFile || !ai">{{ deliveryBusy ? '正在上传…' : '提交正式交付' }}</button>
        </form>
      </div>
      <p v-else class="muted">只有该模块的负责人可以回报进度、提交正式交付。</p>
      <section class="subsection" aria-labelledby="delivery-history-title"><h3 id="delivery-history-title">正式交付版本</h3><p v-if="!currentDeliveries.length" class="empty">该任务还没有正式交付。</p><div class="stack"><article v-for="delivery in currentDeliveries" :key="delivery.id" class="record"><div class="toolbar"><a :href="delivery.download_url" download>{{ delivery.filename }}</a><span class="tag">v{{ delivery.version }} · 已提交</span></div><DeliveryReviewStatus :status="delivery.review_status" :progress="delivery.review_progress" /><DeliveryVersionBinding :delivery="delivery" :members="members" :disabled="busy || loading" :requesting="requestingResubmissionId === delivery.id" @request="requestResubmission(delivery)" /><p class="muted">{{ name(delivery.submitted_by) }} · {{ dateText(delivery.submitted_at) }} · {{ sizeText(delivery.size) }}</p><p>{{ aiLabels[delivery.ai_participation] }}<span v-if="delivery.ai_note">：{{ delivery.ai_note }}</span></p><p v-if="delivery.handoff_note" class="pre-wrap">交接说明：{{ delivery.handoff_note }}</p><details><summary>绑定版本与文件校验</summary><dl class="binding-grid"><dt>方案版本</dt><dd>{{ delivery.plan_version_id }}</dd><dt>规范版本</dt><dd>{{ delivery.spec_version_id }}</dd><dt>SHA256</dt><dd>{{ delivery.sha256 }}</dd></dl></details></article></div></section>
      <details class="subsection progress-history"><summary>全部进度记录</summary><p v-if="historyLoading" class="muted" role="status">正在读取进度…</p><p v-if="historyError" class="error" role="alert">{{ historyError }} <button class="btn quiet" @click="loadHistory()">重试</button></p><p v-if="!reports.length && !historyLoading && !historyError" class="empty">还没有进度记录。</p><ol class="report-list"><li v-for="item in reports" :key="item.id"><strong>{{ stageLabels[item.stage] }}</strong><p class="pre-wrap">{{ item.summary }}</p><span class="muted">{{ progressSource(item) }} · {{ dateText(item.created_at) }}</span></li></ol></details>
    </template>
    <details v-if="archivedDeliveries.length" class="subsection"><summary>已移除模块的历史交付（{{ archivedDeliveries.length }}）</summary><article v-for="delivery in archivedDeliveries" :key="delivery.id" class="record archived-delivery"><div class="toolbar"><a :href="delivery.download_url" download>{{ delivery.filename }}</a><span class="tag">v{{ delivery.version }} · 已提交</span></div><DeliveryReviewStatus :status="delivery.review_status" :progress="delivery.review_progress" /><DeliveryVersionBinding :delivery="delivery" :members="members" :disabled="busy || loading" :requesting="requestingResubmissionId === delivery.id" @request="requestResubmission(delivery)" /><p class="muted">模块 {{ delivery.module_id }} · {{ name(delivery.submitted_by) }} · {{ dateText(delivery.submitted_at) }}</p><p>{{ aiLabels[delivery.ai_participation] }}<span v-if="delivery.ai_note">：{{ delivery.ai_note }}</span></p><p v-if="delivery.handoff_note" class="pre-wrap">交接说明：{{ delivery.handoff_note }}</p><p class="muted binding-text">方案 {{ delivery.plan_version_id }} · 规范 {{ delivery.spec_version_id }}</p></article></details>
    <section class="subsection materials" aria-labelledby="material-title"><div class="panel-heading"><div><h3 id="material-title">共享材料</h3><p class="muted">供全体项目成员查阅，与正式交付分开保存。</p></div></div><form class="material-form" @submit.prevent="uploadMaterial"><div class="field"><label for="material-file">上传共享材料</label><input id="material-file" ref="materialInput" type="file" required :disabled="busy || loading" @change="materialFile = fileFrom($event)"></div><button class="btn" :disabled="busy || loading || !materialFile">{{ materialBusy ? '正在上传…' : '上传材料' }}</button></form><p v-if="!materials.length && !loading && !loadError" class="empty">还没有共享材料，项目成员均可上传。</p><ul class="material-list"><li v-for="material in materials" :key="material.id"><a :href="material.download_url" download>{{ material.filename }}</a><span class="muted">{{ name(material.uploaded_by) }} · {{ dateText(material.uploaded_at) }} · {{ sizeText(material.size) }}</span></li></ul></section>
  </section>
</template>

<style scoped>
.draft-record{overflow-wrap:anywhere}.draft-confirm-form{margin-top:20px;padding-top:20px;border-top:1px solid var(--line)}.draft-confirm-form>.toolbar{justify-content:flex-start}.confirmed-drafts{margin-top:22px}.confirmed-drafts>summary{margin-bottom:16px}.confirmed-drafts .draft-record+.draft-record{margin-top:14px}.module-picker{max-width:560px;margin:24px 0}.task-context{padding:22px;background:var(--canvas);border-left:3px solid var(--teal);margin:24px 0}.task-context h3{margin:0}.context-grid{display:grid;grid-template-columns:92px 1fr;gap:14px;margin:22px 0}.context-grid dt{color:var(--muted);font-size:13px}.context-grid dd{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}.dependency-list{margin:0;padding-left:20px}.dependency-list li+li{margin-top:8px}.latest-report{border-top:1px solid var(--line);padding-top:18px}.latest-report h4{margin:0 0 8px;font-size:13px}.latest-report p{margin:8px 0}.task-forms{display:grid;grid-template-columns:1fr 1fr;gap:20px;align-items:start}.task-forms h3{margin:0}.task-forms .btn{justify-self:start}.subsection{margin-top:30px;border-top:1px solid var(--line);padding-top:24px}.subsection h3{margin-top:0}.subsection summary{cursor:pointer;font-weight:600}.record details{font-size:12px;color:var(--muted)}.binding-grid{display:grid;grid-template-columns:76px 1fr;gap:8px}.binding-grid dd{margin:0;overflow-wrap:anywhere}.report-list{list-style:none;padding:0}.report-list li{padding:16px 0;border-bottom:1px solid var(--line)}.report-list p{margin:8px 0}.archived-delivery{margin-top:16px}.binding-text{overflow-wrap:anywhere}.material-form{display:flex;align-items:end;gap:16px;margin:20px 0}.material-form .field{flex:1}.material-list{padding:0;list-style:none}.material-list li{display:flex;justify-content:space-between;gap:20px;padding:16px 0;border-top:1px solid var(--line);overflow-wrap:anywhere}.material-list span{font-size:12px;text-align:right}@media(max-width:850px){.task-forms{grid-template-columns:1fr}.context-grid{grid-template-columns:1fr;gap:6px}.context-grid dd{margin-bottom:10px}.material-form{align-items:stretch;flex-direction:column}.material-list li{flex-direction:column;gap:6px}.material-list span{text-align:left}}
</style>
