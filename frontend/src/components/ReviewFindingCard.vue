<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { dateText } from '../api/client'
import type { Member, ReviewAnnotationInput, ReviewFinding } from '../api/types'

const props = defineProps<{ finding: ReviewFinding; members: Member[]; canAnnotate?: boolean; busy?: boolean; saving?: boolean; saveSequence?: number; historical?: boolean }>()
const emit = defineEmits<{ annotate: [body: ReviewAnnotationInput]; openDelivery: [deliveryId: string] }>()
const handling = ref<'adopted'|'not_adopted'|''>(''), reason = ref(''), baselineRevision = ref(0)
const baselineHandling = ref(''), baselineReason = ref('')
const categories = { specification: '规范', citation: '文献与真实性', logic: '逻辑', linkage: '衔接' }
const severities = { blocking: '阻断', important: '重要', suggestion: '建议' }
const handlingLabels = { unhandled: '尚未标注', adopted: '采纳', not_adopted: '不采纳' }
const reviewers: Record<string,string> = { format_checker:'程序格式检查',spec_compliance:'规范审核员',citation_database:'文献数据库核查',citation_alignment:'引用对应审核员',logic:'逻辑审核员',linkage:'衔接轻检员' }
const dirty = computed(() => handling.value !== baselineHandling.value || reason.value !== baselineReason.value)
const changed = computed(() => baselineRevision.value !== props.finding.annotation_revision)
const editable = computed(() => !!props.canAnnotate && !props.historical && !props.finding.duplicate_of)
const program = computed(() => props.finding.opinion.source === 'program' || props.finding.opinion.reviewer === 'format_checker')
const sourceLabel = computed(() => props.finding.opinion.reviewer === 'citation_database' || props.finding.opinion.source === 'database' ? '文献数据库核查' : program.value ? '程序检查' : 'AI 生成，供参考')
const location = computed(() => {
  const value = props.finding.opinion.location
  const labels = [value.part, value.page ? `第 ${value.page} 页` : '', value.slide ? `第 ${value.slide} 张幻灯片` : '', value.paragraph ? `第 ${value.paragraph} 段` : '', value.table ? `表 ${value.table}` : '', value.row ? `第 ${value.row} 行` : '', value.column ? `第 ${value.column} 列` : '', value.shape_id ? `形状 ${value.shape_id}` : '', value.line_start ? `行 ${value.line_start}${value.line_end && value.line_end !== value.line_start ? `–${value.line_end}` : ''}` : ''].filter(Boolean)
  return [...labels, value.description].join(' · ')
})
function name(id: string|null) { return props.members.find(member => member.user_id === id)?.username ?? id ?? '未记录' }
function reset() {
  handling.value = props.finding.handling === 'unhandled' ? '' : props.finding.handling
  reason.value = props.finding.reason
  baselineRevision.value = props.finding.annotation_revision
  baselineHandling.value = handling.value; baselineReason.value = reason.value
}
function save() {
  if (!editable.value || props.busy || changed.value || !handling.value || (handling.value === 'not_adopted' && !reason.value.trim())) return
  emit('annotate', { handling: handling.value, reason: reason.value.trim(), expected_revision: baselineRevision.value })
}
watch([() => props.finding.finding_id, () => props.saveSequence], reset, { immediate: true })
watch(() => props.finding.annotation_revision, () => { if (!dirty.value) reset() })
</script>

<template>
  <article class="finding-card" :class="{ historical }" :data-review-finding="finding.finding_id">
    <div class="finding-heading"><div class="finding-labels"><span class="severity" :class="finding.opinion.severity">{{ severities[finding.opinion.severity] }}</span><span class="tag">{{ categories[finding.opinion.category] }}</span><span class="source-label">{{ sourceLabel }}</span></div><button v-if="historical && finding.delivery_id" class="text-button" @click="emit('openDelivery', finding.delivery_id)">查看交付 v{{ finding.delivery_version ?? '历史' }} →</button></div>
    <h4 class="pre-wrap">{{ finding.opinion.problem }}</h4>
    <p class="finding-location">{{ location }}</p>
    <dl class="finding-details"><dt>依据</dt><dd><p v-if="finding.opinion.basis.spec_number">规范条目 {{ finding.opinion.basis.spec_number }}</p><blockquote v-if="finding.opinion.basis.excerpt" class="pre-wrap">{{ finding.opinion.basis.excerpt }}</blockquote><p v-if="finding.opinion.basis.excerpt_verified === false" class="notice" data-excerpt-unverified>正文摘录未能核对，请结合对应片段人工确认。</p><template v-if="finding.opinion.basis.context_excerpt"><p>上下文依据<span v-if="finding.opinion.basis.context_key"> · {{ finding.opinion.basis.context_key }}</span></p><blockquote class="pre-wrap">{{ finding.opinion.basis.context_excerpt }}</blockquote></template><p v-if="finding.opinion.basis.context_excerpt_verified === false" class="notice" data-context-excerpt-unverified>上下文摘录未能核对，请人工确认引用依据。</p></dd><dt>修改建议</dt><dd class="pre-wrap">{{ finding.opinion.suggestion }}</dd></dl>
    <p v-if="finding.duplicate_of" class="duplicate-note">此意见已关联历史不采纳记录，不重复触发退回。原意见及当时理由保留在历史记录中。</p>
    <div class="handling-record"><strong>{{ handlingLabels[finding.handling] }}</strong><span v-if="finding.actor_id">{{ name(finding.actor_id) }} · {{ dateText(finding.annotated_at) }}</span><p v-if="finding.reason" class="pre-wrap">{{ finding.reason }}</p></div>
    <form v-if="editable" class="annotation-form" @submit.prevent="save">
      <div class="annotation-fields"><div class="field"><label :for="`handling-${finding.finding_id}`">本人的处理标注</label><select :id="`handling-${finding.finding_id}`" v-model="handling" :disabled="busy" required><option value="" disabled>请选择采纳或不采纳</option><option value="adopted">采纳，修改后重新提交</option><option value="not_adopted">不采纳，填写理由</option></select></div><div class="field"><label :for="`reason-${finding.finding_id}`">{{ handling === 'not_adopted' ? '不采纳理由（必填）' : '处理说明（选填）' }}</label><textarea :id="`reason-${finding.finding_id}`" v-model="reason" :disabled="busy" :required="handling === 'not_adopted'" maxlength="4000" rows="3"/></div></div>
      <p v-if="changed" class="notice" role="status">其他人已更新此标注。你的输入已保留，请先核对上方最新记录。<button type="button" class="text-button" :disabled="busy" @click="baselineRevision = finding.annotation_revision">已核对，按最新记录继续编辑</button></p>
      <button class="btn" :data-annotation-save="finding.finding_id" :disabled="busy || changed || !handling || (handling === 'not_adopted' && !reason.trim())">{{ saving ? '正在保存…' : '保存标注' }}</button>
    </form>
    <details class="finding-provenance"><summary>审核来源</summary><dl><dt>审核员</dt><dd>{{ reviewers[finding.opinion.reviewer ?? ''] ?? finding.opinion.reviewer ?? sourceLabel }}</dd><dt>指令版本</dt><dd>{{ finding.opinion.instruction_version ?? '未提供' }}</dd><dt>模型</dt><dd>{{ finding.opinion.model ?? (program ? '程序检查，不调用模型' : '不适用') }}</dd><template v-if="finding.opinion.input_hash"><dt>输入摘要哈希</dt><dd>{{ finding.opinion.input_hash }}</dd></template><template v-if="finding.opinion.call_id"><dt>调用记录</dt><dd>{{ finding.opinion.call_id }}</dd></template><template v-if="finding.duplicate_of"><dt>关联原意见</dt><dd>{{ finding.duplicate_of }}</dd></template></dl></details>
  </article>
</template>

<style scoped>
.finding-card{border:1px solid var(--line);padding:22px;overflow-wrap:anywhere;background:var(--paper)}.finding-heading,.finding-labels{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.finding-heading{justify-content:space-between}.source-label,.finding-location{font-size:12px;color:var(--muted)}.severity{font-size:12px;font-weight:600;padding:3px 8px;border-left:3px solid var(--line)}.severity.blocking{border-color:#a33d32;color:#8b342b}.severity.important{border-color:#986319;color:#795018}.severity.suggestion{border-color:var(--teal);color:var(--teal)}.finding-card h4{font-size:16px;line-height:1.7;margin:18px 0 8px}.finding-location{line-height:1.7}.finding-details{display:grid;grid-template-columns:70px 1fr;gap:14px;font-size:13px;line-height:1.8;margin:20px 0}.finding-details dt{color:var(--muted)}.finding-details dd{margin:0}.finding-details p{margin:0}.finding-details blockquote{margin:8px 0 0;padding-left:12px;border-left:2px solid var(--line);color:var(--muted)}.handling-record{padding:14px 0;border-top:1px solid var(--line);font-size:12px}.handling-record span{margin-left:12px;color:var(--muted)}.handling-record p{font-size:13px;line-height:1.8;margin-bottom:0}.annotation-form{padding-top:8px}.annotation-fields{display:grid;grid-template-columns:minmax(200px,1fr) 2fr;gap:16px;align-items:start}.annotation-form>.btn{margin-top:12px}.annotation-form .notice .text-button{display:block;margin-top:8px}.duplicate-note{font-size:12px;line-height:1.8;padding:12px;background:var(--canvas);border-left:2px solid var(--teal)}.finding-provenance{margin-top:18px;color:var(--muted);font-size:12px}.finding-provenance summary{cursor:pointer}.finding-provenance dl{display:grid;grid-template-columns:90px 1fr;gap:8px;line-height:1.7}.finding-provenance dd{margin:0}.historical{background:var(--canvas)}@media(max-width:700px){.finding-card{padding:16px}.finding-details{grid-template-columns:1fr;gap:6px}.finding-details dd{margin-bottom:10px}.annotation-fields{grid-template-columns:1fr}.finding-provenance dl{grid-template-columns:1fr;gap:4px}.finding-provenance dd{margin-bottom:8px}.handling-record span{display:block;margin:6px 0 0}}
</style>
