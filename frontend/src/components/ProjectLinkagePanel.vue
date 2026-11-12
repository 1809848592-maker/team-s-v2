<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ApiError, api, dateText, errorText, projectPath, utcDate } from '../api/client'
import type { CollaborationRequest, Member, Module, ProjectLinkageFinding, ProjectLinkageReview } from '../api/types'
import ProjectLinkageReport from './ProjectLinkageReport.vue'

const props = defineProps<{ projectId: string; accountId: string; leader: boolean; members: Member[]; modules: Module[] }>()
const emit = defineEmits<{ changed: []; openRequests: [] }>()
const reviews = ref<ProjectLinkageReview[]>([]), selectedId = ref(''), detail = ref<ProjectLinkageReview | null>(null)
const loading = ref(false), detailLoading = ref(false), busy = ref(false), error = ref(''), detailError = ref(''), actionError = ref(''), notice = ref('')
const conversion = ref<ProjectLinkageFinding | null>(null), title = ref(''), content = ref(''), targetKind = ref<'person' | 'module'>('module'), targetId = ref(''), expectedBy = ref('')
const visible = ref(typeof document === 'undefined' || document.visibilityState !== 'hidden')
const statusNames = { queued: '等待后台处理', running: '后台处理中', completed: '后台处理完成', partial_failed: '部分失败，审核未完成' }
const active = computed(() => reviews.value.some(item => ['queued', 'running'].includes(item.status)))
const base = () => `${projectPath(props.projectId)}/linkage-reviews`
const currentKey = () => `${props.projectId}:${props.accountId}`
let epoch = 0, sequence = 0, detailSequence = 0, mounted = true, blocked = false, failures = 0
let timer: ReturnType<typeof setTimeout> | undefined, loadController: AbortController | undefined, detailController: AbortController | undefined, mutationController: AbortController | undefined
function name(id: string) { return props.members.find(member => member.user_id === id)?.username ?? id }
function clearTimer() { if (timer !== undefined) clearTimeout(timer); timer = undefined }
function schedule() {
  clearTimer()
  if (mounted && visible.value && active.value && !blocked) timer = setTimeout(() => void load(false), Math.min(3000 * Math.max(1, failures), 15000))
}
function update(item: ProjectLinkageReview) {
  const exists = reviews.value.some(review => review.id === item.id)
  reviews.value = exists ? reviews.value.map(review => review.id === item.id ? item : review) : [item, ...reviews.value]
  if (selectedId.value === item.id) detail.value = item
}
async function load(manual = true) {
  clearTimer()
  if (busy.value || !mounted || (!manual && (!visible.value || blocked))) { schedule(); return }
  if (manual) { blocked = false; failures = 0 }
  const key = currentKey(), owner = epoch, requestSequence = ++sequence
  const current = () => mounted && owner === epoch && key === currentKey() && requestSequence === sequence
  loadController?.abort()
  const controller = new AbortController(); loadController = controller
  let timedOut = false
  const deadline = setTimeout(() => { timedOut = true; controller.abort() }, 15000)
  loading.value = true
  try {
    const result = await api<ProjectLinkageReview[]>(base(), undefined, undefined, controller.signal)
    if (!current()) return
    reviews.value = result; error.value = ''; failures = 0
    if (!result.some(item => item.id === selectedId.value)) selectedId.value = result[0]?.id ?? ''
    else if (selectedId.value && !conversion.value) await loadDetail()
  } catch (cause) {
    if (!current()) return
    error.value = timedOut ? '读取衔接审核进度超时，请重试。' : errorText(cause); ++failures
    if (cause instanceof ApiError && [401, 403, 404].includes(cause.status)) blocked = true
  } finally {
    clearTimeout(deadline)
    if (loadController === controller) loadController = undefined
    if (current()) { loading.value = false; schedule() }
  }
}
async function loadDetail() {
  const key = currentKey(), owner = epoch, id = selectedId.value, requestSequence = ++detailSequence
  const current = () => mounted && owner === epoch && key === currentKey() && id === selectedId.value && requestSequence === detailSequence
  detailController?.abort(); detailError.value = ''
  if (!id) { detail.value = null; detailLoading.value = false; return }
  const controller = new AbortController(); detailController = controller
  let timedOut = false
  const deadline = setTimeout(() => { timedOut = true; controller.abort() }, 15000)
  detailLoading.value = true
  try {
    const result = await api<ProjectLinkageReview>(`${base()}/${encodeURIComponent(id)}`, undefined, undefined, controller.signal)
    if (current()) update(result)
  } catch (cause) {
    if (current()) detailError.value = timedOut ? '读取衔接审核详情超时，请重试。' : errorText(cause)
  } finally {
    clearTimeout(deadline)
    if (detailController === controller) detailController = undefined
    if (current()) detailLoading.value = false
  }
}
async function run(retry = false) {
  if (!props.leader || busy.value || loading.value || detailLoading.value || (retry && !detail.value?.can_retry)) return
  const key = currentKey(), owner = epoch, id = detail.value?.id
  const current = () => mounted && owner === epoch && key === currentKey()
  const controller = new AbortController(); mutationController = controller
  busy.value = true; actionError.value = ''; notice.value = ''; clearTimer()
  try {
    const result = await api<ProjectLinkageReview>(retry ? `${base()}/${encodeURIComponent(id!)}/retry` : base(), undefined, 'POST', controller.signal)
    if (!current()) return
    update(result); selectedId.value = result.id; detail.value = result; conversion.value = null
    notice.value = retry ? '已请求重试，仍使用这份审核冻结的方案与交付版本。' : '完整衔接审核已排队，先生成模块摘要，再做整体比对。'
    emit('changed')
  } catch (cause) { if (current()) actionError.value = errorText(cause) }
  finally {
    if (mutationController === controller) mutationController = undefined
    if (current()) { busy.value = false; schedule() }
  }
}
function openConversion(finding: ProjectLinkageFinding) {
  if (!props.leader || !detail.value?.can_convert || finding.request_id || busy.value) return
  conversion.value = finding
  title.value = `衔接审核：${finding.problem}`.slice(0, 200)
  content.value = '请核对该审核意见，完成修订后在需求中回应处理结果。'
  const moduleId = finding.module_ids.find(id => props.modules.some(module => module.id === id))
  targetKind.value = 'module'; targetId.value = moduleId ?? ''; expectedBy.value = ''; actionError.value = ''
}
async function createRequest() {
  if (!props.leader || !detail.value?.can_convert || !conversion.value || busy.value || !targetId.value || !title.value.trim() || !content.value.trim()) return
  const key = currentKey(), owner = epoch, id = selectedId.value, finding = conversion.value
  const current = () => mounted && owner === epoch && key === currentKey() && id === selectedId.value
  const controller = new AbortController(); mutationController = controller
  busy.value = true; actionError.value = ''; notice.value = ''; clearTimer()
  try {
    const result = await api<CollaborationRequest>(`${base()}/${encodeURIComponent(id)}/findings/${encodeURIComponent(finding.id)}/request`, {
      title: title.value.trim(), content: content.value.trim(),
      target_user_id: targetKind.value === 'person' ? targetId.value : null,
      target_module_id: targetKind.value === 'module' ? targetId.value : null,
      expected_by: utcDate(expectedBy.value),
    }, undefined, controller.signal)
    if (!current()) return
    if (detail.value?.result) detail.value.result.findings = detail.value.result.findings.map(item => item.id === finding.id ? { ...item, request_id: result.id } : item)
    conversion.value = null; notice.value = '已转为协作需求，问题来源随需求保留。'; emit('changed')
    await loadDetail()
  } catch (cause) { if (current()) actionError.value = errorText(cause) }
  finally {
    if (mutationController === controller) mutationController = undefined
    if (current()) { busy.value = false; schedule() }
  }
}
function onVisibilityChange() { visible.value = document.visibilityState !== 'hidden'; if (visible.value) schedule(); else clearTimer() }
watch(targetKind, () => { targetId.value = '' }, { flush: 'sync' })
watch(selectedId, () => { detail.value = null; conversion.value = null; actionError.value = ''; void loadDetail() })
watch(() => props.leader, value => { if (!value) conversion.value = null })
watch(currentKey, () => {
  ++epoch; ++sequence; ++detailSequence; clearTimer(); loadController?.abort(); detailController?.abort(); mutationController?.abort()
  reviews.value = []; selectedId.value = ''; detail.value = null; conversion.value = null; loading.value = false; detailLoading.value = false; busy.value = false
  error.value = ''; detailError.value = ''; actionError.value = ''; notice.value = ''; blocked = false; failures = 0
  void load()
}, { immediate: true })
onMounted(() => document.addEventListener('visibilitychange', onVisibilityChange))
onBeforeUnmount(() => {
  mounted = false; ++epoch; ++sequence; ++detailSequence; clearTimer(); loadController?.abort(); detailController?.abort(); mutationController?.abort()
  document.removeEventListener('visibilitychange', onVisibilityChange)
})
</script>

<template>
  <section class="panel" aria-labelledby="project-linkage-title">
    <div class="panel-heading"><div><p class="eyebrow">模块摘要 · 项目整体比对</p><h2 id="project-linkage-title">完整衔接审核</h2></div><div class="toolbar"><button class="btn quiet" :disabled="loading || busy" @click="load()">刷新报告</button><button v-if="leader" class="btn primary" :disabled="loading || busy || detailLoading" @click="run()">{{ busy ? '正在处理…' : '发起完整衔接审核' }}</button></div></div>
    <p class="muted linkage-intro">按当前方案逐模块选取最新已采用交付，先生成摘要，再检查模块衔接、重复、遗漏、矛盾及方案执行情况。没有已采用交付的模块标为未覆盖。</p>
    <p v-if="!leader" class="muted">由项目负责人发起审核并将意见分派为协作需求，项目成员可查看报告。</p>
    <p v-if="error" class="error" role="alert">{{ error }} <button class="btn quiet" :disabled="loading || busy" @click="load()">重试读取</button></p><p v-if="actionError" class="error" role="alert">{{ actionError }}</p><p v-if="notice" class="notice" role="status">{{ notice }} <button v-if="notice.includes('已转为协作需求')" type="button" class="btn quiet" @click="$emit('openRequests')">查看协作需求 →</button></p>
    <p v-if="loading && !reviews.length" class="muted" role="status">正在读取衔接审核记录…</p><p v-if="!reviews.length && !loading && !error" class="empty">尚无完整衔接审核报告。{{ leader ? '整合前或终审时，可以发起一次项目审核。' : '负责人发起后，报告将在这里显示。' }}</p>
    <div v-if="reviews.length" class="linkage-layout">
      <nav class="linkage-history" aria-label="衔接审核历史"><button v-for="item in reviews" :key="item.id" type="button" class="history-choice" :class="{ selected: selectedId === item.id }" :aria-current="selectedId === item.id ? 'true' : undefined" :disabled="busy" @click="selectedId = item.id"><strong>方案 v{{ item.plan_version }} · 规范 v{{ item.spec_version }}</strong><span>{{ statusNames[item.status] }}</span><time :datetime="item.created_at">{{ dateText(item.created_at) }}</time></button></nav>
      <div class="linkage-detail" :aria-busy="detailLoading"><p v-if="detailLoading && !detail" class="muted" role="status">正在读取报告详情…</p><p v-if="detailError" class="error" role="alert">{{ detailError }} <button class="btn quiet" :disabled="busy || detailLoading" @click="loadDetail()">重试详情</button></p>
        <template v-if="detail"><div class="toolbar"><h3>项目衔接审核报告</h3><span class="tag">{{ statusNames[detail.status] }}</span></div><p class="muted report-byline">{{ name(detail.requested_by) }} 发起 · {{ dateText(detail.created_at) }} · 方案 v{{ detail.plan_version }} / 规范 v{{ detail.spec_version }}</p><p class="state-note" role="status">{{ detail.message }}</p><p v-if="['queued','running'].includes(detail.status)" class="muted">正在后台处理，进度会自动刷新。第 {{ detail.attempt }} / {{ detail.max_attempts }} 次处理。</p><p v-if="detail.status === 'partial_failed'" class="coverage-warning">审核尚未完成，不能视作通过。{{ detail.error_code ? `错误标识：${detail.error_code}` : '' }}</p><button v-if="leader && detail.can_retry" type="button" class="btn" :disabled="busy || loading || detailLoading" @click="run(true)">重试未完成审核</button>
          <ProjectLinkageReport v-if="detail.result" :result="detail.result" :can-convert="leader && detail.can_convert" :disabled="busy || detailLoading" @convert="openConversion" @open-request="$emit('openRequests')"/><p v-else-if="detail.status === 'partial_failed'" class="empty">尚无有效报告，失败范围保留为未完成。</p>
        </template>
      </div>
    </div>
    <section v-if="conversion" class="conversion-dialog" role="dialog" aria-modal="false" aria-labelledby="linkage-request-title"><div class="panel-heading"><h3 id="linkage-request-title">将审核意见转为协作需求</h3><button type="button" class="btn quiet" :disabled="busy" @click="conversion = null">取消</button></div><p class="muted">确认需求对象及内容后发起。原审核报告、问题和依据会随需求保留。</p><form class="stack" @submit.prevent="createRequest"><div class="field"><label for="linkage-request-name">需求标题</label><input id="linkage-request-name" v-model="title" required maxlength="200" :disabled="busy"></div><div class="field"><label for="linkage-request-content">需求内容</label><textarea id="linkage-request-content" v-model="content" rows="5" required maxlength="10000" :disabled="busy"/></div><div class="form-grid"><div class="field"><label for="linkage-request-kind">对象类型</label><select id="linkage-request-kind" v-model="targetKind" :disabled="busy"><option value="module">模块</option><option value="person">成员</option></select></div><div class="field"><label for="linkage-request-target">{{ targetKind === 'module' ? '指定模块' : '指定成员' }}</label><select id="linkage-request-target" v-model="targetId" required :disabled="busy"><option disabled value="">请选择</option><template v-if="targetKind === 'module'"><option v-for="module in modules" :key="module.id" :value="module.id">{{ module.name }}</option></template><template v-else><option v-for="member in members" :key="member.user_id" :value="member.user_id">{{ member.username }}</option></template></select></div><div class="field"><label for="linkage-request-expected">期望回应时间（选填）</label><input id="linkage-request-expected" v-model="expectedBy" type="datetime-local" :disabled="busy"></div></div><button class="btn primary" :disabled="busy || !targetId || !title.trim() || !content.trim()">{{ busy ? '正在发起…' : '确认并发起协作需求' }}</button></form></section>
  </section>
</template>

<style scoped>
.linkage-intro{max-width:850px;line-height:1.8}.linkage-layout{display:grid;grid-template-columns:minmax(200px,.65fr) minmax(0,2fr);gap:26px;margin-top:26px}.linkage-history{display:flex;flex-direction:column;gap:10px;align-self:start}.history-choice{display:flex;flex-direction:column;gap:10px;padding:15px;border:1px solid var(--line);border-radius:6px;background:white;color:inherit;text-align:left;cursor:pointer;font:inherit;font-size:13px}.history-choice.selected{border-color:var(--teal);background:#f4faf9;box-shadow:inset 3px 0 var(--teal)}.history-choice:disabled{cursor:wait}.history-choice time{font-size:11px;color:var(--muted)}.linkage-detail{min-width:0;border-left:1px solid var(--line);padding-left:26px}.linkage-detail h3{font-size:17px;margin:0}.report-byline{font-size:12px;line-height:1.8}.state-note{padding:12px 14px;background:var(--canvas);font-size:13px;line-height:1.7}.coverage-warning{padding:12px 14px;background:#fff8e9;border-left:3px solid #d4a75c;color:#7a4e15;font-size:13px;line-height:1.7}.conversion-dialog{margin-top:26px;border:1px solid var(--teal);border-radius:7px;padding:22px;background:#f9fcfb}.conversion-dialog h3{font-size:16px;margin:0}.conversion-dialog>.muted{font-size:13px;line-height:1.8}.conversion-dialog form>.btn{justify-self:start}@media(max-width:950px){.linkage-layout{grid-template-columns:1fr}.linkage-history{max-height:280px;overflow:auto}.linkage-detail{border-left:0;border-top:1px solid var(--line);padding:24px 0 0}}
</style>
