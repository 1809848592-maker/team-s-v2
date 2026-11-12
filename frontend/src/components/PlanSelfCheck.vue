<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ApiError, api, errorText, projectPath } from '../api/client'
import type { PlanContent, PlanSelfCheckResult, ProjectModelDisclosure } from '../api/types'
import ModelDisclosureNotice from './ModelDisclosureNotice.vue'

const props = defineProps<{ projectId: string; accountId: string; leader: boolean; content: PlanContent }>()
const disclosure = ref<ProjectModelDisclosure | null>(null)
const disclosureLoading = ref(false)
const error = ref('')
const consentChecked = ref(false)
const consenting = ref(false)
const checking = ref(false)
const result = ref<PlanSelfCheckResult | null>(null)
const reviewedSnapshot = ref('')
const stale = computed(() => !!result.value && reviewedSnapshot.value !== JSON.stringify(props.content))
const labels: Record<string, string> = { dependency: '依赖闭合', omission: '职责缺失', overlap: '职责重叠', alignment: '论证主线与模块目标对齐' }
const statuses: Record<string, string> = { issues: '有审核意见', checked: '已检查所给材料', uncovered: '未覆盖' }
let generation = 0
let alive = true
let controller: AbortController | null = null
let disclosureSequence = 0
function current(sequence: number) { return alive && props.leader && sequence === generation }

async function loadDisclosure() {
  const sequence = generation, requestSequence = ++disclosureSequence
  disclosureLoading.value = true
  try {
    const value = await api<ProjectModelDisclosure>(`${projectPath(props.projectId)}/model-disclosure`, undefined, undefined, controller?.signal)
    if (current(sequence) && requestSequence === disclosureSequence) disclosure.value = value
  } catch (cause) {
    if (current(sequence) && requestSequence === disclosureSequence) error.value = errorText(cause)
  } finally {
    if (current(sequence) && requestSequence === disclosureSequence) disclosureLoading.value = false
  }
}

async function consent() {
  if (!props.leader || !consentChecked.value || !disclosure.value?.can_consent || consenting.value || checking.value) return
  const sequence = generation
  consenting.value = true
  error.value = ''
  try {
    const value = await api<ProjectModelDisclosure>(`${projectPath(props.projectId)}/model-consent`, { disclosure_version: disclosure.value.notice.version }, undefined, controller?.signal)
    if (current(sequence)) { disclosure.value = value; consentChecked.value = false }
  } catch (cause) {
    if (current(sequence)) {
      error.value = errorText(cause)
      if (cause instanceof ApiError && cause.status === 409) await loadDisclosure()
    }
  } finally { if (current(sequence)) consenting.value = false }
}

async function check() {
  if (!props.leader || checking.value || consenting.value || disclosureLoading.value || !disclosure.value?.project_consent) return
  const sequence = generation, snapshot = JSON.stringify(props.content)
  checking.value = true
  error.value = ''
  // A failed new attempt must not leave an older report looking like its result.
  result.value = null
  try {
    const value = await api<PlanSelfCheckResult>(`${projectPath(props.projectId)}/plan/self-check`, {
      request_id: crypto.randomUUID(), disclosure_version: disclosure.value.notice.version, content: JSON.parse(snapshot),
    }, undefined, controller?.signal)
    if (current(sequence)) { result.value = value; reviewedSnapshot.value = snapshot }
  } catch (cause) {
    if (current(sequence)) {
      error.value = `方案自检未完成。${errorText(cause)}`
      if (cause instanceof ApiError && cause.status === 409) await loadDisclosure()
    }
  } finally { if (current(sequence)) checking.value = false }
}

watch(() => [props.projectId, props.accountId, props.leader], () => {
  ++generation
  controller?.abort()
  controller = new AbortController()
  disclosure.value = null; consentChecked.value = false; consenting.value = false
  checking.value = false; result.value = null; reviewedSnapshot.value = ''; error.value = ''
  if (props.leader) void loadDisclosure()
}, { immediate: true })
onBeforeUnmount(() => { alive = false; ++generation; controller?.abort() })
</script>

<template>
  <section v-if="leader" class="stack plan-self-check" aria-label="方案自检">
    <div><h3>方案自检（可选）</h3><p class="muted">检查当前草稿的依赖、职责和论证对齐。AI 生成，供参考；仅供参考，不阻止发布。</p></div>
    <p v-if="disclosureLoading" class="muted" role="status">正在读取数据外发告知…</p>
    <template v-if="disclosure">
      <ModelDisclosureNotice :notice="disclosure.notice" compact />
      <div v-if="!disclosure.project_consent" class="stack">
        <p class="muted">负责人确认本版数据外发告知后可使用方案自检。</p>
        <label v-if="disclosure.can_consent" class="consent-checkbox"><input v-model="consentChecked" type="checkbox" :disabled="consenting">我已阅读并同意以上数据外发说明。</label>
        <button v-if="disclosure.can_consent" class="btn quiet" type="button" :disabled="!consentChecked || consenting" @click="consent">确认数据外发</button>
      </div>
    </template>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <button v-if="!disclosure && !disclosureLoading" class="btn quiet" type="button" @click="loadDisclosure">重新读取外发告知</button>
    <div class="toolbar"><button class="btn quiet" type="button" :disabled="checking || consenting || disclosureLoading || !disclosure?.project_consent" @click="check">{{ checking ? '方案自检中…' : '方案自检' }}</button><span v-if="checking" class="muted" role="status">可继续编辑草稿或发布，结果仅对应发起时的草稿。</span></div>
    <section v-if="result" class="stack" aria-label="方案自检结果">
      <p v-if="stale" class="notice" role="status">草稿已修改，以下意见对应之前的草稿；请按需重新自检。</p>
      <p class="notice">{{ result.status === 'completed' ? '已完成本次自检。' : '本次自检存在未完成或未覆盖范围，不作通过判断。' }}AI 生成，供参考；可继续编辑和发布。</p>
      <ul class="compact-list"><li v-for="item in result.checks" :key="item.key">{{ labels[item.key] }} · {{ statuses[item.status] }}：{{ item.reason }}</li></ul>
      <p v-if="result.discarded_findings" class="muted">{{ result.discarded_findings }} 条意见因格式无效未采用。</p>
      <article v-for="(finding, index) in result.findings" :key="index" class="stack">
        <strong>{{ labels[finding.check_key] }} · {{ finding.severity === 'important' ? '重要意见' : '建议' }} · {{ finding.location }}</strong>
        <p>{{ finding.problem }}</p><blockquote>{{ finding.excerpt }}</blockquote>
        <p v-if="!finding.excerpt_verified || finding.context_excerpt_verified === false" class="muted">摘录未能核对，请结合当前方案人工确认。</p>
        <p v-if="finding.context_excerpt" class="muted">上下文依据：{{ finding.context_excerpt }}</p>
        <p>修改建议：{{ finding.suggestion }}</p>
        <details><summary>审核来源</summary><p class="muted">{{ finding.reviewer }} · {{ finding.instruction_version }} · {{ finding.model }} · 输入 {{ finding.input_hash }}</p></details>
      </article>
      <p v-if="!result.findings.length" class="muted">本次未生成审核意见；请结合检查覆盖范围人工确认。</p>
      <ul v-if="result.coverage_notes.length" class="muted"><li v-for="(note, index) in result.coverage_notes" :key="index">{{ note }}</li></ul>
    </section>
  </section>
</template>

<style scoped>
.plan-self-check{border:1px solid var(--line);padding:20px;border-radius:6px}.plan-self-check h3{margin:0}.plan-self-check article{border-top:1px solid var(--line);padding-top:16px}.plan-self-check blockquote{margin:0;padding:10px 14px;border-left:3px solid var(--line);white-space:pre-wrap;overflow-wrap:anywhere}.plan-self-check details p{overflow-wrap:anywhere}
</style>
