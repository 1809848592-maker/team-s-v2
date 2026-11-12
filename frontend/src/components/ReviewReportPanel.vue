<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ApiError, api, errorText, projectPath } from '../api/client'
import type { Delivery, Member, ReviewAnnotationInput, ReviewFinding, ReviewProgress, ReviewReport } from '../api/types'
import DeliveryReviewStatus from './DeliveryReviewStatus.vue'
import ReviewFindingCard from './ReviewFindingCard.vue'
import FinalReviewPanel from './FinalReviewPanel.vue'

const props = defineProps<{ projectId: string; accountId: string; delivery: Delivery; members: Member[]; disabled?: boolean }>()
const emit = defineEmits<{ changed: []; openDelivery: [deliveryId: string] }>()
const report = ref<ReviewReport|null>(null), loading = ref(false), retrying = ref(false), savingId = ref('')
const loadError = ref(''), actionError = ref(''), notice = ref('')
const saves = ref<Record<string,number>>({}), finalSequence = ref(0)
let epoch = 0, sequence = 0, mounted = true, controller: AbortController|undefined, mutationController: AbortController|undefined
const busy = computed(() => loading.value || retrying.value || !!savingId.value || !!props.disabled)
const state = computed(() => report.value?.state ?? props.delivery.review_status)
const progress = computed(() => report.value?.review_progress ?? props.delivery.review_progress)
const canRetry = computed(() => report.value?.can_retry && report.value.workflow_revision != null && progress.value?.status === 'failed')
const completed = computed(() => report.value?.coverage === 'completed' || report.value?.coverage === 'bypassed')
const finalKey = computed(() => `${report.value?.report_id ?? ''}:${report.value?.workflow_revision ?? ''}:${finalSequence.value}`)
const coverageLabels: Record<string,string> = {not_covered:'尚未形成报告',completed:'审核流程已完成',incomplete:'审核尚未完成',bypassed:'AI 审核未收敛，转交终审'}
const statusLabels: Record<string,string> = {completed:'已完成',incomplete:'未完成',passed:'有限检查未发现问题',partial:'有限覆盖',unsupported:'此文件类型未覆盖',not_applicable:'不适用，未作通过判断',checked:'已核对',issues:'发现不符合或需确认项',error:'未完成',no_ai_items:'没有需 AI 检查的条目',uncovered:'未覆盖',delegated:'由程序或人工处理',compliant:'未发现不符合',noncompliant:'有审核意见',not_found:'查无此条',verified:'检索到匹配记录',unverified:'未能核验',matched:'已对应',uncited:'未在正文引用',unavailable:'来源暂不可用',no_match:'未匹配记录'}
const stageNames: Record<string,string> = {format:'程序格式检查',specification:'规范审核',citations:'文献核查',reasoning:'逻辑与衔接轻检'}
type Data = Record<string,unknown>
function object(value: unknown): Data { return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Data : {} }
function text(value: unknown): string { return typeof value === 'string' ? value : '' }
function list(value: unknown): unknown[] { return Array.isArray(value) ? value : [] }
function texts(value: unknown): string[] { return list(value).map(item => typeof item === 'string' ? item : text(object(item).message) || text(object(item).reason)).filter(Boolean) }
function unique(values: string[]) { return [...new Set(values)] }
function status(value: unknown) { const raw = text(value); return statusLabels[raw] ?? (raw || '未提供状态') }
function reasoningLabel(key: string) {
  const labels: Record<string,string> = {goal:'模块目标',argument:'论证主线',support:'论据与推断',scope:'模块边界',terms:'术语表',data_definitions:'数据口径表',upstream:'上游交接'}
  if (labels[key]) return labels[key]
  const [kind,identity] = key.split(':', 2)
  if ((kind === 'term' || kind === 'data') && identity && /^\d+$/.test(identity)) return `${kind === 'term' ? '术语' : '数据口径'}条目 ${Number(identity) + 1}`
  return kind === 'upstream' && identity ? `上游模块 ${identity}` : key
}
const stages = computed(() => Object.entries(stageNames).map(([key,label]) => {
  const data = object(report.value?.components?.[key])
  const children = ['review','extraction','alignment','logic','linkage'].map(name => ({name,data:object(data[name])})).filter(child => Object.keys(child.data).length)
  const checked: string[] = [], uncovered: string[] = []
  for (const item of [data, ...children.map(child => child.data)]) {
    const coverage = object(item.coverage)
    checked.push(...texts(coverage.checked)); uncovered.push(...texts(coverage.uncovered))
    if (Array.isArray(item.coverage)) uncovered.push(...texts(item.coverage))
  }
  const checkTitle = ({format:'逐条程序规范覆盖',specification:'逐条规范覆盖',citations:'逐条引用对应检查',reasoning:'逐项逻辑与衔接检查'} as Record<string,string>)[key] ?? '逐项检查'
  const rows = key === 'reasoning'
    ? ['logic','linkage'].flatMap(reviewer => list(object(data.checks)[reviewer]).map(item => ({check:object(item),reviewer})))
    : list(data.checks).map(item => ({check:object(item),reviewer:''}))
  const checks = rows.map(({check,reviewer}) => {
    const identity = key === 'citations' ? text(check.reference_id) : key === 'reasoning' ? text(check.key) : text(check.spec_number) || text(check.number)
    const checkLabel = key === 'citations' ? `文献 ${identity || '未提供编号'}` : key === 'reasoning' ? `${reviewer === 'logic' ? '逻辑' : '衔接'} · ${reasoningLabel(identity) || '未提供检查项'}` : identity
    return {id:reviewer ? `${reviewer}:${identity}` : identity,label:checkLabel,requirement:text(check.requirement),state:check.status === 'issues' && key !== 'format' ? '有审核意见' : status(check.status),reason:text(check.reason),coverage:texts(check.coverage)}
  })
  const errors = unique([data, ...children.map(child => child.data)].map(item => text(item.error_code)).filter(Boolean))
  return {key,label,present:Object.keys(data).length > 0,state:status(data.status),summary:text(data.summary),checked:unique(checked),uncovered:unique(uncovered),checkTitle,checks,errors,warnings:unique(texts(data.extraction_warnings)),children:children.map(child => ({name:({review:'规范审核员',extraction:'参考文献抽取',alignment:'引用对应',logic:'逻辑审核员',linkage:'衔接轻检员'} as Record<string,string>)[child.name] ?? child.name,state:status(child.data.status)}))}
}))
const references = computed(() => list(object(report.value?.components?.citations).references).map(item => {
  const reference = object(item), verification = object(reference.verification)
  return {id:text(reference.reference_id),title:text(reference.title) || text(reference.raw_reference) || '未提供题名',state:status(verification.status),unverified:verification.status === 'unverified',reason:text(verification.reason),sources:list(verification.sources).map(item => {const source = object(item); return {provider:text(source.provider),state:status(source.status),reason:text(source.reason)}})}
}))
function owner() { return {epoch,projectId:props.projectId,accountId:props.accountId,deliveryId:props.delivery.id} }
function current(value: ReturnType<typeof owner>) { return mounted && value.epoch === epoch && value.projectId === props.projectId && value.accountId === props.accountId && value.deliveryId === props.delivery.id }
function path(value = owner()) { return `${projectPath(value.projectId)}/deliveries/${encodeURIComponent(value.deliveryId)}/review` }
async function load() {
  const value = owner(), requestSequence = ++sequence
  controller?.abort()
  const requestController = new AbortController(); controller = requestController
  const timeout = setTimeout(() => requestController.abort(), 15000)
  loading.value = true; loadError.value = ''
  try {
    const result = await api<ReviewReport>(path(value), undefined, undefined, requestController.signal)
    if (current(value) && requestSequence === sequence) report.value = result
  } catch (error) {
    if (current(value) && requestSequence === sequence) loadError.value = requestController.signal.aborted ? '读取审核报告超时，请重试。' : errorText(error)
  } finally {
    clearTimeout(timeout)
    if (controller === requestController) controller = undefined
    if (current(value) && requestSequence === sequence) loading.value = false
  }
}
async function annotate(finding: ReviewFinding, body: ReviewAnnotationInput) {
  if (busy.value || loadError.value || !report.value?.can_annotate || finding.duplicate_of) return
  const value = owner()
  const mutation = new AbortController(); mutationController = mutation
  savingId.value = finding.finding_id; actionError.value = ''; notice.value = ''
  try {
    const result = await api<ReviewFinding>(`${path(value)}/findings/${encodeURIComponent(finding.finding_id)}/annotation`, body, undefined, mutation.signal)
    if (!current(value)) return
    report.value = report.value ? {...report.value,opinions:report.value.opinions.map(item => item.finding_id === result.finding_id ? result : item)} : null
    saves.value = {...saves.value,[result.finding_id]:(saves.value[result.finding_id] ?? 0) + 1}
    ++finalSequence.value; notice.value = '标注已保存。采纳项请在新版本中修改后重新提交；标注本身不改变交付状态。'
    await load(); if (current(value)) emit('changed')
  } catch (error) {
    if (!current(value)) return
    actionError.value = error instanceof ApiError && error.status === 409 ? '审核意见或处理记录已有更新。你的输入已保留，请核对最新记录后再保存。' : errorText(error)
    if (error instanceof ApiError && error.status === 409) { await load(); if (current(value)) ++finalSequence.value }
  } finally { if (mutationController === mutation) mutationController = undefined; if (current(value)) savingId.value = '' }
}
async function retryReview() {
  if (busy.value || loadError.value || !canRetry.value || report.value?.workflow_revision == null) return
  const value = owner(), revision = report.value.workflow_revision
  const mutation = new AbortController(); mutationController = mutation
  retrying.value = true; actionError.value = ''; notice.value = ''
  try {
    await api<ReviewProgress>(`${path(value)}/retry`, {expected_revision:revision}, undefined, mutation.signal)
    if (!current(value)) return
    notice.value = '审核重试已排队，等待后台处理；当前尚无新的审核结论。'
    await load(); if (current(value)) emit('changed')
  } catch (error) {
    if (!current(value)) return
    actionError.value = error instanceof ApiError && error.status === 409 ? '审核状态已有更新，请核对刷新后的进度；未再次发起重试。' : errorText(error)
    if (error instanceof ApiError && error.status === 409) await load()
  } finally { if (mutationController === mutation) mutationController = undefined; if (current(value)) retrying.value = false }
}
async function finalChanged() { const value = owner(); ++finalSequence.value; await load(); if (current(value)) emit('changed') }
watch([() => props.projectId,() => props.accountId,() => props.delivery.id], () => {
  ++epoch; ++sequence; controller?.abort(); mutationController?.abort()
  report.value = null; loading.value = false; retrying.value = false; savingId.value = ''; loadError.value = ''; actionError.value = ''; notice.value = ''; saves.value = {}; finalSequence.value = 0
  void load()
}, {immediate:true})
watch(() => [props.delivery,props.members.map(member => `${member.user_id}:${member.role}`).join(',')], () => { if (!savingId.value && !retrying.value) void load() })
onBeforeUnmount(() => { mounted = false; ++epoch; ++sequence; controller?.abort(); mutationController?.abort() })
</script>

<template>
  <section class="review-report-panel" :data-review-report="delivery.id" aria-label="交付审核报告">
    <div class="panel-heading"><div><p class="eyebrow">交付 v{{ delivery.version }} · {{ delivery.filename }}</p><h3>审核报告</h3></div><button class="btn quiet" :disabled="busy" @click="load()">刷新报告</button></div>
    <p class="review-reference">审核意见中由 AI 生成的内容供参考，未覆盖范围仍需人工确认。程序检查与文献数据库核查会分别标明来源。</p>
    <DeliveryReviewStatus :status="state" :progress="progress"/>
    <p v-if="loading" class="muted" role="status">正在读取审核报告…</p>
    <p v-if="loadError" class="error" role="alert">{{ loadError }} <button class="btn quiet" :disabled="busy" @click="load()">重试读取报告</button></p>
    <p v-if="actionError" class="error" role="alert">{{ actionError }}</p><p v-if="notice" class="notice" role="status">{{ notice }}</p>
    <template v-if="report">
      <div class="report-conclusion" :class="{incomplete:report.coverage === 'incomplete' || report.coverage === 'not_covered'}"><strong>{{ coverageLabels[report.coverage] ?? report.coverage }}</strong><span v-if="report.round" class="muted">第 {{ report.round }} 轮</span><p>{{ report.note }}</p></div>
      <div v-if="report.coverage === 'incomplete' || progress?.status === 'failed'" class="retry-action"><p class="muted">未完成的审核不能视为通过。已完成步骤的诊断保留，失败步骤恢复后可重新审核。</p><button v-if="canRetry" class="btn" data-review-retry :disabled="busy || !!loadError" @click="retryReview">{{ retrying ? '正在安排重试…' : '重试审核' }}</button><p v-else-if="progress?.status === 'failed'" class="muted">仅提交人及当前模块负责人可重试；当前状态或权限不满足重试条件。</p></div>
      <section v-if="report.components && Object.keys(report.components).length" class="coverage-section" aria-label="审核覆盖与未覆盖范围"><h4>各阶段覆盖范围</h4><div class="stage-list"><details v-for="stage in stages" :key="stage.key" class="stage-coverage" :data-review-stage="stage.key" :open="stage.errors.length > 0"><summary><strong>{{ stage.label }}</strong><span>{{ stage.present ? stage.state : '尚无本阶段报告' }}</span></summary><p v-if="stage.summary" class="pre-wrap">{{ stage.summary }}</p><p v-for="child in stage.children" :key="child.name" class="muted">{{ child.name }}：{{ child.state }}</p><p v-if="stage.errors.length" class="error">此阶段未完成：{{ stage.errors.join('；') }}。不能作为通过结论。</p><template v-if="stage.checked.length"><h5>已覆盖</h5><ul><li v-for="item in stage.checked" :key="item" class="pre-wrap">{{ item }}</li></ul></template><template v-if="stage.uncovered.length"><h5>未覆盖／覆盖边界</h5><ul class="uncovered"><li v-for="item in stage.uncovered" :key="item" class="pre-wrap">{{ item }}</li></ul></template><template v-if="stage.warnings.length"><h5>文本抽取提示</h5><ul><li v-for="item in stage.warnings" :key="item" class="pre-wrap">{{ item }}</li></ul></template><p v-if="stage.present && !stage.checked.length && !stage.uncovered.length && !stage.checks.length && !stage.warnings.length" class="muted">本阶段未提供逐项覆盖说明，请结合意见及人工核对。</p><details v-if="stage.checks.length" class="check-details"><summary>{{ stage.checkTitle }}（{{ stage.checks.length }}）</summary><ol><li v-for="(check,index) in stage.checks" :key="`${check.id}:${index}`" :data-review-check="check.id"><strong>{{ check.label || `条目 ${index + 1}` }} · {{ check.state }}</strong><p v-if="check.requirement" class="pre-wrap">{{ check.requirement }}</p><p v-if="check.reason" class="pre-wrap">{{ check.reason }}</p><ul v-if="check.coverage.length"><li v-for="item in check.coverage" :key="item" class="pre-wrap">{{ item }}</li></ul></li></ol></details></details></div></section>
      <details v-if="references.length" class="reference-details"><summary>参考文献数据库核查（{{ references.length }}）</summary><p class="muted">数据库匹配不证明全文或论断真实；“未能核验”不表示文献不存在。</p><ol><li v-for="(reference,index) in references" :key="`${reference.id}:${index}`"><div class="toolbar"><strong>{{ reference.id }}</strong><span class="tag" :class="{unverified:reference.unverified}">{{ reference.state }}</span></div><p class="pre-wrap">{{ reference.title }}</p><p class="pre-wrap muted">{{ reference.reason }}</p><ul v-if="reference.sources.length"><li v-for="source in reference.sources" :key="source.provider">{{ source.provider }} · {{ source.state }}<p class="pre-wrap muted">{{ source.reason }}</p></li></ul></li></ol></details>
      <section class="opinions-section" aria-label="本轮审核意见"><div class="toolbar"><h4>本轮审核意见</h4><span class="tag">{{ report.opinions.length }} 条</span></div><p v-if="!completed && report.opinions.length" class="muted">审核未完成，以下仅为诊断记录，本轮尚不接受处理标注。</p><p v-if="!report.opinions.length" class="empty">{{ completed ? '本轮没有新的审核意见；仍请核对历史意见和未覆盖范围。' : '尚无可展示的逐条审核意见，不能据此视为通过。' }}</p><p v-if="report.can_annotate && completed" class="muted">提交人及同模块共同负责人可以标注，系统会记录实际标注人。不采纳必须填写理由；建议级意见可选标注。</p><div class="stack"><ReviewFindingCard v-for="finding in report.opinions" :key="finding.finding_id" :finding="finding" :members="members" :can-annotate="completed && report.can_annotate" :busy="busy || !!loadError" :saving="savingId === finding.finding_id" :save-sequence="saves[finding.finding_id] ?? 0" @annotate="annotate(finding,$event)"/></div></section>
      <details class="history-opinions"><summary>历史意见与处理记录（{{ report.history?.length ?? 0 }}）</summary><p v-if="report.history_truncated" class="notice">此处最多展示500条历史意见。其余记录请打开对应旧交付版本查阅；终审会独立读取完整依据。</p><p v-if="!report.history?.length" class="empty">没有历史意见。</p><div class="stack"><ReviewFindingCard v-for="finding in report.history ?? []" :key="finding.finding_id" :finding="finding" :members="members" historical @open-delivery="emit('openDelivery',$event)"/></div></details>
      <FinalReviewPanel v-if="completed" :project-id="projectId" :account-id="accountId" :delivery-id="delivery.id" :members="members" :refresh-key="finalKey" :disabled="busy || !!loadError" @changed="finalChanged" @open-delivery="emit('openDelivery',$event)"/>
    </template>
  </section>
</template>

<style scoped>
.review-report-panel{margin-top:26px;padding:26px;border:1px solid var(--line);border-top:3px solid var(--teal);background:var(--paper);overflow-wrap:anywhere}.review-report-panel h3{margin:0}.review-reference{font-size:12px;color:var(--muted);line-height:1.8}.report-conclusion{border-left:3px solid var(--teal);background:var(--canvas);padding:16px 18px;margin:22px 0;font-size:13px;line-height:1.8}.report-conclusion>span{margin-left:12px}.report-conclusion p{margin:8px 0 0}.report-conclusion.incomplete{border-color:#986319}.retry-action{margin:20px 0}.coverage-section{margin:26px 0}.coverage-section h4,.opinions-section h4{font-size:15px;margin:0 0 14px}.stage-list{border-top:1px solid var(--line)}.stage-coverage{border-bottom:1px solid var(--line);padding:15px 0;font-size:13px;line-height:1.8}.stage-coverage>summary{cursor:pointer}.stage-coverage>summary>strong{margin-right:18px}.stage-coverage>summary>span{color:var(--muted);font-size:12px}.stage-coverage h5{font-size:12px;margin:18px 0 8px}.stage-coverage ul{padding-left:22px;margin:8px 0}.stage-coverage .uncovered{color:#795018}.check-details{margin-top:18px}.check-details summary{cursor:pointer}.check-details ol{padding-left:24px}.check-details ol>li{padding:12px 0;border-bottom:1px solid var(--line)}.check-details p{margin:8px 0}.reference-details{margin:24px 0;font-size:13px;line-height:1.8}.reference-details>summary,.history-opinions>summary{cursor:pointer;font-weight:600;margin-bottom:16px}.reference-details>ol{padding-left:24px}.reference-details>ol>li{padding:14px 0;border-top:1px solid var(--line)}.reference-details p{margin:8px 0}.reference-details .unverified{color:#795018}.opinions-section{border-top:1px solid var(--line);padding-top:24px;margin-top:24px}.history-opinions{margin-top:28px}.history-opinions .stack{margin-top:18px}@media(max-width:700px){.review-report-panel{padding:18px 14px}.stage-coverage>summary>span{display:block;margin-left:18px}.report-conclusion{padding:14px}.report-conclusion>span{display:block;margin:4px 0 0}}
</style>
