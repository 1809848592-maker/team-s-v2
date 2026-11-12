<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { ApiError, api, errorText } from '../api/client'
import type { AdminProject, AdminUsage } from '../api/types'

const props = defineProps<{ accountId: string }>()
const today = new Date().toISOString().slice(0, 10)
const weekAgo = new Date(Date.now() - 6 * 86400000).toISOString().slice(0, 10)
const start = ref(weekAgo), end = ref(today), projectId = ref('')
const report = ref<AdminUsage | null>(null), projects = ref<AdminProject[]>([])
const loading = ref(false), error = ref(''), actionError = ref(''), notice = ref(''), exporting = ref(false)
const savingId = ref(''), limits = ref<Record<string, string>>({})
let sequence = 0, mounted = true
const reviewers: Record<string, string> = {spec_compliance:'规范审核',citation_extract:'参考文献抽取',citation_alignment:'引用对应检查',logic:'逻辑审核',linkage:'衔接轻检',spec_draft:'规范辅助起草',project_summary:'项目模块摘要',project_linkage:'完整衔接审核'}
function number(value: number) { return value.toLocaleString('zh-CN') }
function query() {
  const first = Date.parse(`${start.value}T00:00:00Z`), last = Date.parse(`${end.value}T00:00:00Z`)
  if (!/^\d{4}-\d{2}-\d{2}$/.test(start.value) || !/^\d{4}-\d{2}-\d{2}$/.test(end.value) || !Number.isFinite(first) || !Number.isFinite(last) || first > last || last - first > 365 * 86400000) throw new Error('请选择首尾顺序正确、最多 366 天的日期范围。')
  const params = new URLSearchParams({start:start.value,end:end.value})
  if (projectId.value) params.set('project_id', projectId.value)
  return params.toString()
}
async function load() {
  const owner = ++sequence, account = props.accountId
  const current = () => mounted && owner === sequence && account === props.accountId
  let params: string
  try { params = query() } catch (cause) { error.value = errorText(cause); return }
  loading.value = true; error.value = ''; report.value = null
  try {
    const [data, available] = await Promise.all([api<AdminUsage>(`/api/admin/usage?${params}`),api<AdminProject[]>('/api/admin/projects')])
    if (!current()) return
    report.value = data; projects.value = available
    limits.value = Object.fromEntries(available.map(item => [item.id, String(item.daily_review_limit)]))
  } catch (cause) { if (current()) error.value = errorText(cause) }
  finally { if (current()) loading.value = false }
}
async function saveQuota(project: AdminProject) {
  if (savingId.value || loading.value) return
  const value = limits.value[project.id] ?? ''
  if (!/^\d+$/.test(value) || !Number.isSafeInteger(Number(value)) || Number(value) > 2147483647) { actionError.value = '每日审核额度须为 0 至 2147483647 的整数。'; return }
  const account = props.accountId
  savingId.value = project.id; actionError.value = ''; notice.value = ''
  try {
    const updated = await api<AdminProject>(`/api/admin/projects/${encodeURIComponent(project.id)}/quota`, {daily_review_limit:Number(value)}, 'PUT')
    if (!mounted || account !== props.accountId) return
    projects.value = projects.value.map(item => item.id === updated.id ? updated : item)
    limits.value[updated.id] = String(updated.daily_review_limit)
    notice.value = `${updated.name} 的每日审核额度已更新为 ${updated.daily_review_limit}。`
  } catch (cause) { if (mounted && account === props.accountId) actionError.value = errorText(cause) }
  finally { if (mounted && account === props.accountId) savingId.value = '' }
}
async function exportCsv() {
  if (exporting.value || loading.value) return
  const account = props.accountId
  let params: string
  try { params = query() } catch (cause) { actionError.value = errorText(cause); return }
  exporting.value = true; actionError.value = ''; notice.value = ''
  try {
    const response = await fetch(`/api/admin/usage.csv?${params}`, {credentials:'same-origin',cache:'no-store'})
    if (!response.ok) {
      if (response.status === 401) window.dispatchEvent(new Event('session-expired'))
      const payload = await response.json().catch(() => null)
      throw new ApiError(response.status, typeof payload?.detail === 'string' ? payload.detail : '导出未完成，请重试。')
    }
    const file = await response.blob()
    if (!mounted || account !== props.accountId) return
    const url = URL.createObjectURL(file), link = document.createElement('a')
    link.href = url; link.download = `模型用量-${start.value}-${end.value}.csv`; link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
    notice.value = '用量明细已导出，实际金额请与 DeepSeek 平台账单核对。'
  } catch (cause) { if (mounted && account === props.accountId) actionError.value = errorText(cause) }
  finally { if (mounted && account === props.accountId) exporting.value = false }
}
watch(() => props.accountId, () => { ++sequence; projects.value = []; report.value = null; error.value = ''; actionError.value = ''; notice.value = ''; savingId.value = ''; exporting.value = false; void load() }, {immediate:true})
onBeforeUnmount(() => { mounted = false; ++sequence })
</script>

<template>
  <section class="panel admin-usage" aria-labelledby="admin-usage-title">
    <div class="panel-heading"><div><p class="eyebrow">平台管理</p><h1 id="admin-usage-title">模型用量与额度</h1></div></div>
    <p class="muted">按 UTC 日期统计，包含首日和末日。费用为人民币估算，仅供报销对照，实际金额以 DeepSeek 平台账单为准。</p>
    <form class="usage-filters" @submit.prevent="load">
      <div class="field"><label for="usage-start">开始日期（UTC）</label><input id="usage-start" v-model="start" type="date" required :disabled="loading || exporting"></div>
      <div class="field"><label for="usage-end">结束日期（UTC）</label><input id="usage-end" v-model="end" type="date" required :disabled="loading || exporting"></div>
      <div class="field"><label for="usage-project">项目范围</label><select id="usage-project" v-model="projectId" :disabled="loading || exporting"><option value="">全部项目</option><option v-for="project in projects" :key="project.id" :value="project.id">{{ project.name }}</option></select></div>
      <button class="btn primary" :disabled="loading || !!savingId">{{ loading ? '正在读取…' : '查询用量' }}</button>
      <button type="button" class="btn" :disabled="loading || exporting || !!savingId" @click="exportCsv">{{ exporting ? '正在导出…' : '导出 CSV 明细' }}</button>
    </form>
    <p v-if="loading" class="muted" role="status">正在读取用量记录…</p>
    <p v-if="error" class="error" role="alert">{{ error }} <button class="btn quiet" :disabled="loading" @click="load">重试</button></p>
    <p v-if="actionError" class="error" role="alert">{{ actionError }}</p><p v-if="notice" class="notice" role="status">{{ notice }}</p>
    <template v-if="report">
      <p class="muted">本次结果：{{ report.start }} 至 {{ report.end }}（UTC，含首尾）。</p>
      <div class="usage-totals" aria-label="用量合计"><div><span>模型调用</span><strong>{{ number(report.totals.calls) }}</strong></div><div><span>已知 token 合计</span><strong>{{ number(report.totals.total_tokens) }}</strong></div><div><span>已知费用估算（元）</span><strong>¥{{ report.totals.estimated_cost_cny }}</strong></div></div>
      <p v-if="report.totals.unknown_usage_calls || report.totals.unpriced_calls || report.totals.unknown_cache_calls" class="incomplete-usage" role="status">记录尚不完整：{{ report.totals.unknown_usage_calls }} 次用量未知，{{ report.totals.unknown_cache_calls }} 次缓存拆分未知，{{ report.totals.unpriced_calls }} 次费用未知。合计仅包含已知数值。</p>
      <p v-if="!report.totals.calls" class="empty">所选范围内没有模型调用记录。</p>
      <template v-else>
        <h2>按项目</h2><div class="table-scroll"><table><thead><tr><th scope="col">项目</th><th scope="col">调用次数</th><th scope="col">输入 token</th><th scope="col">输出 token</th><th scope="col">已知 token 合计</th><th scope="col">已知估算（元）</th><th scope="col">未知用量 / 费用</th></tr></thead><tbody><tr v-for="row in report.by_project" :key="row.project_id"><th scope="row">{{ row.project_name }}</th><td>{{ number(row.calls) }}</td><td>{{ number(row.prompt_tokens) }}</td><td>{{ number(row.completion_tokens) }}</td><td>{{ number(row.total_tokens) }}</td><td>¥{{ row.estimated_cost_cny }}</td><td>{{ row.unknown_usage_calls }} / {{ row.unpriced_calls }}</td></tr></tbody></table></div>
        <h2>按审核员</h2><div class="table-scroll"><table><thead><tr><th scope="col">审核员</th><th scope="col">调用次数</th><th scope="col">输入 token</th><th scope="col">输出 token</th><th scope="col">已知 token 合计</th><th scope="col">已知估算（元）</th><th scope="col">未知用量 / 费用</th></tr></thead><tbody><tr v-for="row in report.by_reviewer" :key="row.reviewer"><th scope="row">{{ reviewers[row.reviewer] ?? row.reviewer }}</th><td>{{ number(row.calls) }}</td><td>{{ number(row.prompt_tokens) }}</td><td>{{ number(row.completion_tokens) }}</td><td>{{ number(row.total_tokens) }}</td><td>¥{{ row.estimated_cost_cny }}</td><td>{{ row.unknown_usage_calls }} / {{ row.unpriced_calls }}</td></tr></tbody></table></div>
      </template>
      <section class="quota-section"><h2>每日审核额度</h2><p class="muted">平台当前每日上限：{{ number(report.limits.platform_daily_reviews) }} 份 AI 处理名额、{{ number(report.limits.platform_daily_tokens) }} token；单份审核最多 {{ report.limits.max_calls_per_review }} 次模型调用。平台上限由部署配置管理。</p><p class="muted">一份正式交付审核任务计 1 份名额，一次规范辅助起草计 1 份名额，共用项目与平台每日额度。设为 0 会暂停新的处理请求；已获名额的审核仍可继续，已用额度不会清零。</p>
        <p v-if="!projects.length" class="empty">平台尚无项目。</p><div v-else class="table-scroll"><table><thead><tr><th scope="col">项目</th><th scope="col">当前每日额度</th><th scope="col">调整额度</th></tr></thead><tbody><tr v-for="project in projects" :key="project.id"><th scope="row">{{ project.name }}</th><td>{{ number(project.daily_review_limit) }}</td><td><form class="quota-form" @submit.prevent="saveQuota(project)"><label :for="`quota-${project.id}`" class="sr-only">{{ project.name }} 的每日审核额度</label><input :id="`quota-${project.id}`" v-model="limits[project.id]" type="number" min="0" max="2147483647" step="1" required :disabled="loading || !!savingId"><button class="btn" :disabled="loading || !!savingId">{{ savingId === project.id ? '正在保存…' : '保存额度' }}</button></form></td></tr></tbody></table></div>
      </section>
    </template>
  </section>
</template>

<style scoped>
.admin-usage h1{font-size:28px}.usage-filters{display:flex;flex-wrap:wrap;align-items:end;gap:14px;margin:24px 0}.usage-filters .field{flex:1 1 160px}.usage-totals{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;margin:24px 0}.usage-totals>div{padding:20px;background:var(--canvas);border:1px solid var(--line);border-radius:5px;display:grid;gap:12px}.usage-totals span{font-size:12px;color:var(--muted)}.usage-totals strong{font-size:24px;font-weight:600;overflow-wrap:anywhere}.incomplete-usage{padding:16px;border-left:3px solid #ad8433;background:#fff7e7;font-size:13px;line-height:1.7}.admin-usage h2{font-size:17px;margin:30px 0 16px}.table-scroll{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:14px 12px;border-bottom:1px solid var(--line);vertical-align:middle;white-space:nowrap}thead th{background:var(--canvas);font-weight:600}tbody th{white-space:normal;min-width:150px;font-weight:500;overflow-wrap:anywhere}.quota-section{border-top:1px solid var(--line);margin-top:32px;padding-top:4px}.quota-form{display:flex;gap:12px;align-items:center}.quota-form input{width:140px}.quota-form .btn{white-space:nowrap}@media(max-width:750px){.usage-totals{grid-template-columns:1fr}.usage-filters .btn{flex:1}.quota-form input{width:100px}}
</style>
