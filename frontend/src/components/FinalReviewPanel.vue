<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ApiError, api, dateText, errorText, projectPath, reviewStatusText } from '../api/client'
import type { FinalReviewContext, FinalReviewDecision, Member, SpecificationRuling } from '../api/types'
import ReviewFindingCard from './ReviewFindingCard.vue'

const props = defineProps<{ projectId: string; accountId: string; deliveryId: string; members: Member[]; refreshKey?: string; disabled?: boolean }>()
const emit = defineEmits<{ changed: []; openDelivery: [deliveryId: string] }>()
const context = ref<FinalReviewContext|null>(null), loading = ref(false), saving = ref(false)
const loadError = ref(''), actionError = ref(''), notice = ref('')
const decision = ref<'approve'|'return'|''>(''), comment = ref('')
const rulings = ref<Record<string,{decision:'accept'|'reject'|'';comment:string}>>({})
const acceptedHash = ref(''), needsRecheck = ref(false)
let epoch = 0, sequence = 0, mounted = true, controller: AbortController|undefined, mutationController: AbortController|undefined
const dirty = computed(() => !!decision.value || !!comment.value || Object.values(rulings.value).some(item => !!item.decision || !!item.comment))
const exceptions = computed(() => context.value?.specification_exceptions ?? [])
const otherRejected = computed(() => (context.value?.not_adopted ?? []).filter(item => item.opinion.category !== 'specification'))
const hasRejectedRuling = computed(() => exceptions.value.some(item => rulings.value[item.finding_id]?.decision === 'reject'))
const invalidRulings = computed(() => exceptions.value.some(item => {
  const value = rulings.value[item.finding_id]
  return (decision.value === 'approve' && !value?.decision) || (value?.decision === 'reject' && !value.comment.trim())
}))
const canSubmit = computed(() => context.value?.can_decide && !loading.value && !saving.value && !loadError.value && !props.disabled && !needsRecheck.value && acceptedHash.value === context.value.context_hash && !!decision.value && (decision.value !== 'return' || !!comment.value.trim()) && !invalidRulings.value)
function name(id: string) { return props.members.find(member => member.user_id === id)?.username ?? id }
function owner() { return { epoch, projectId: props.projectId, accountId: props.accountId, deliveryId: props.deliveryId } }
function current(value: ReturnType<typeof owner>) { return mounted && value.epoch === epoch && value.projectId === props.projectId && value.accountId === props.accountId && value.deliveryId === props.deliveryId }
function path(value = owner()) { return `${projectPath(value.projectId)}/deliveries/${encodeURIComponent(value.deliveryId)}/review/final` }
async function load(forceRecheck = false) {
  const value = owner(), requestSequence = ++sequence
  controller?.abort()
  const requestController = new AbortController(); controller = requestController
  const timeout = setTimeout(() => requestController.abort(), 15000)
  loading.value = true; loadError.value = ''
  try {
    const result = await api<FinalReviewContext>(path(value), undefined, undefined, requestController.signal)
    if (!current(value) || requestSequence !== sequence) return
    const changed = !!acceptedHash.value && acceptedHash.value !== result.context_hash
    context.value = result
    const next = { ...rulings.value }
    for (const item of result.specification_exceptions) next[item.finding_id] ??= {decision:'',comment:''}
    rulings.value = next
    if ((changed && dirty.value) || forceRecheck) needsRecheck.value = true
    else if (!needsRecheck.value) acceptedHash.value = result.context_hash
  } catch (error) {
    if (current(value) && requestSequence === sequence) loadError.value = requestController.signal.aborted ? '读取终审依据超时，请重试。' : errorText(error)
  } finally {
    clearTimeout(timeout)
    if (controller === requestController) controller = undefined
    if (current(value) && requestSequence === sequence) loading.value = false
  }
}
function acknowledge() {
  if (!context.value || loading.value || saving.value) return
  acceptedHash.value = context.value.context_hash; needsRecheck.value = false
  notice.value = '已确认使用当前终审依据。请核对保留的决定与意见后提交。'
}
async function submit() {
  if (!canSubmit.value || !context.value || !decision.value) return
  const value = owner(), snapshot = context.value
  const mutation = new AbortController(); mutationController = mutation
  const specRulings: SpecificationRuling[] = exceptions.value.flatMap(item => {
    const ruling = rulings.value[item.finding_id]
    return ruling?.decision ? [{finding_id:item.finding_id,decision:ruling.decision,comment:ruling.comment.trim()}] : []
  })
  saving.value = true; actionError.value = ''; notice.value = ''
  try {
    await api<FinalReviewDecision>(path(value), {decision:decision.value,comment:comment.value.trim(),expected_revision:snapshot.workflow_revision,context_hash:acceptedHash.value,spec_rulings:specRulings}, undefined, mutation.signal)
    if (!current(value)) return
    decision.value = ''; comment.value = ''; rulings.value = {}; needsRecheck.value = false; acceptedHash.value = ''
    notice.value = '终审决定已记录。'; await load()
    if (current(value)) emit('changed')
  } catch (error) {
    if (!current(value)) return
    actionError.value = error instanceof ApiError && error.status === 409 ? '审核状态或处理记录已有更新。你的输入已保留，请重新核对当前终审依据。' : errorText(error)
    if (error instanceof ApiError && error.status === 409) await load(true)
  } finally { if (mutationController === mutation) mutationController = undefined; if (current(value)) saving.value = false }
}
watch([() => props.projectId, () => props.accountId, () => props.deliveryId], () => {
  ++epoch; ++sequence; controller?.abort(); mutationController?.abort()
  context.value = null; loading.value = false; saving.value = false; loadError.value = ''; actionError.value = ''; notice.value = ''
  decision.value = ''; comment.value = ''; rulings.value = {}; acceptedHash.value = ''; needsRecheck.value = false
  void load()
}, {immediate:true})
watch(() => props.refreshKey, () => { if (!saving.value) void load() })
watch(() => props.members.map(member => `${member.user_id}:${member.role}`).join(','), () => { if (!saving.value) void load() })
onBeforeUnmount(() => { mounted = false; ++epoch; ++sequence; controller?.abort(); mutationController?.abort() })
</script>

<template>
  <section class="final-review" data-final-review aria-label="负责人终审">
    <div class="panel-heading"><div><p class="eyebrow">由人负责</p><h3>负责人终审</h3></div><button class="btn quiet" :disabled="loading || saving || disabled" @click="load()">刷新终审依据</button></div>
    <p v-if="loading" class="muted" role="status">正在读取终审依据…</p>
    <p v-if="loadError" class="error" role="alert">{{ loadError }} <button class="btn quiet" :disabled="loading || saving" @click="load()">重试读取终审依据</button></p>
    <p v-if="actionError" class="error" role="alert">{{ actionError }}</p><p v-if="notice" class="notice" role="status">{{ notice }}</p>
    <template v-if="context">
      <p class="muted">交付 v{{ context.delivery_version }} · {{ reviewStatusText(context.state) }}。任何一名有权限的项目负责人作出终审决定即可。</p>
      <p v-if="context.nonconverged" class="notice">AI 审核未收敛，第 {{ context.round }} 轮。请结合交付、未覆盖范围和历史处理记录完成终审。</p>
      <p v-if="context.self_review" class="notice" data-self-review>项目当前只有一名负责人，本次终审将明确记录为“自审”。</p>
      <p v-else-if="!context.can_decide && !context.decision" class="muted">{{ context.leader_ids.includes(accountId) && context.submitted_by === accountId ? '项目有多名负责人，提交人不能终审自己的交付，请由另一名负责人完成。' : context.state === 'pending_final_review' ? '当前账号可以查阅终审依据，决定须由有权限的项目负责人完成。' : '本版本当前不接受终审决定。' }}</p>
      <section class="exceptions-section" aria-label="规范不适用与不采纳项"><h4>规范不适用／不采纳项 <span>{{ exceptions.length }}</span></h4><p class="muted">以下规范类不采纳理由须由负责人逐条裁定。任一项不予认可，本次终审结果即为退回，并须附上意见。</p><p v-if="!exceptions.length" class="empty">没有需要裁定的规范例外项。</p>
        <article v-for="item in exceptions" :key="item.finding_id" class="spec-exception" :data-spec-exception="item.finding_id"><div class="toolbar"><strong>{{ item.opinion.basis.spec_number ? `规范 ${item.opinion.basis.spec_number}` : '规范审核意见' }}</strong><button v-if="item.delivery_id" class="text-button" @click="emit('openDelivery', item.delivery_id)">交付 v{{ item.delivery_version }} →</button></div><p class="pre-wrap">{{ item.opinion.problem }}</p><blockquote v-if="item.opinion.basis.excerpt" class="pre-wrap">{{ item.opinion.basis.excerpt }}</blockquote><p class="exception-reason pre-wrap">不采纳理由：{{ item.reason }}</p><p class="muted">{{ item.actor_id ? name(item.actor_id) : '未记录' }} · {{ dateText(item.annotated_at) }}</p>
          <div v-if="context.can_decide && rulings[item.finding_id]" class="ruling-fields"><div class="field"><label :for="`ruling-${item.finding_id}`">负责人裁定</label><select :id="`ruling-${item.finding_id}`" v-model="rulings[item.finding_id]!.decision" :disabled="saving || loading || disabled"><option value="" disabled>请选择</option><option value="accept">接受该不适用／不采纳理由</option><option value="reject">不予认可，终审退回</option></select></div><div class="field"><label :for="`ruling-comment-${item.finding_id}`">{{ rulings[item.finding_id]!.decision === 'reject' ? '驳回意见（必填）' : '裁定说明（选填）' }}</label><textarea :id="`ruling-comment-${item.finding_id}`" v-model="rulings[item.finding_id]!.comment" maxlength="4000" rows="2" :disabled="saving || loading || disabled"/></div></div>
        </article>
      </section>
      <details class="rejected-history" :open="otherRejected.length > 0"><summary>其他不采纳意见（{{ otherRejected.length }}）</summary><p v-if="!otherRejected.length" class="empty">没有其他不采纳意见。</p><div class="stack"><ReviewFindingCard v-for="item in otherRejected" :key="item.finding_id" :finding="item" :members="members" historical @open-delivery="emit('openDelivery', $event)"/></div></details>
      <section v-if="context.decision" class="final-decision" data-final-decision aria-label="已记录的终审决定"><div class="toolbar"><h4>{{ context.decision.outcome === 'adopted' ? '终审通过，已采用' : '终审退回' }}</h4><span v-if="context.decision.self_review" class="tag">自审</span></div><p class="muted">{{ name(context.decision.actor_id) }} · {{ dateText(context.decision.created_at) }}</p><p v-if="context.decision.comment" class="pre-wrap">{{ context.decision.comment }}</p><p v-else class="muted">未附总意见。</p><ul v-if="context.decision.spec_rulings.length" class="decision-rulings"><li v-for="item in context.decision.spec_rulings" :key="item.finding_id"><strong>{{ item.decision === 'accept' ? '接受规范例外' : '驳回规范例外' }}</strong><span class="muted"> · {{ context.specification_exceptions.find(entry => entry.finding_id === item.finding_id)?.opinion.basis.spec_number || item.finding_id }}</span><p v-if="item.comment" class="pre-wrap">{{ item.comment }}</p></li></ul><p class="muted">本次决定及当时的依据已保留。返工请重新提交新版本。</p></section>
      <form v-else-if="context.can_decide" class="final-form" @submit.prevent="submit">
        <p v-if="needsRecheck" class="notice" role="status">终审依据已有更新，你的决定、意见和逐条裁定已保留。请核对当前列表后再继续。<button type="button" class="text-button" :disabled="loading || saving" @click="acknowledge">已核对，使用当前终审依据</button></p>
        <div class="field"><label :for="`final-decision-${deliveryId}`">终审决定</label><select :id="`final-decision-${deliveryId}`" v-model="decision" :disabled="saving || loading || disabled" required><option value="" disabled>请选择终审决定</option><option value="approve">通过并采用</option><option value="return">退回修改</option></select></div>
        <div class="field"><label :for="`final-comment-${deliveryId}`">{{ decision === 'return' ? '终审退回意见（必填）' : '终审总意见（选填）' }}</label><textarea :id="`final-comment-${deliveryId}`" v-model="comment" maxlength="4000" rows="4" :required="decision === 'return'" :disabled="saving || loading || disabled"/></div>
        <p v-if="hasRejectedRuling" class="notice">已有规范例外项不予认可，本次终审将退回修改。</p><p v-else class="muted">通过时须完成全部规范例外项裁定。退回时须写明返工意见。</p>
        <button class="btn primary" data-final-submit :disabled="!canSubmit">{{ saving ? '正在记录终审…' : decision === 'return' || hasRejectedRuling ? '确认终审退回' : '确认终审通过' }}</button>
      </form>
    </template>
  </section>
</template>

<style scoped>
.final-review{margin-top:30px;padding-top:26px;border-top:2px solid var(--teal)}.final-review h3{margin:0}.exceptions-section{margin:24px 0;border-left:3px solid #986319;padding:6px 0 6px 18px}.exceptions-section h4{font-size:15px;margin:0 0 12px}.exceptions-section h4 span{font-size:12px;color:var(--muted);margin-left:8px}.spec-exception{border-top:1px solid var(--line);padding:18px 0;font-size:13px;line-height:1.8}.spec-exception blockquote{margin:12px 0;padding-left:12px;border-left:2px solid var(--line);color:var(--muted)}.exception-reason{background:var(--canvas);padding:12px}.ruling-fields{display:grid;grid-template-columns:minmax(220px,1fr) 2fr;gap:16px;margin-top:16px}.rejected-history{margin:24px 0}.rejected-history summary{cursor:pointer;font-weight:600;margin-bottom:18px}.final-form{display:grid;gap:18px;border-top:1px solid var(--line);padding-top:24px}.final-form>.btn{justify-self:start}.final-form .notice .text-button{display:block;margin-top:10px}.final-decision{background:var(--canvas);border-top:1px solid var(--line);padding:22px;margin-top:24px;overflow-wrap:anywhere}.final-decision h4{margin:0}.decision-rulings{padding-left:20px;font-size:13px}.decision-rulings li{padding:10px 0}.decision-rulings p{margin-bottom:0}@media(max-width:700px){.ruling-fields{grid-template-columns:1fr}.exceptions-section{padding-left:12px}.final-decision{padding:16px}}
</style>
