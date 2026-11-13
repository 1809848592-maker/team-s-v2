import type { DeliveryReviewStatus, ProjectEvent, ReviewProgress } from './types'

export class ApiError extends Error {
  constructor(public readonly status: number, message: string) { super(message) }
}
export async function api<T>(path: string, body?: unknown, method?: string, signal?: AbortSignal): Promise<T> {
  const form = body instanceof FormData
  const response = await fetch(path, {
    method: method ?? (body === undefined ? 'GET' : 'POST'),
    credentials: 'same-origin', cache: 'no-store', signal,
    headers: body !== undefined || method === 'POST'
      ? { 'X-CSRF-Protection': '1', ...(!form && body !== undefined ? { 'Content-Type': 'application/json' } : {}) } : {},
    body: body === undefined ? undefined : form ? body : JSON.stringify(body),
  })
  if (!response.ok) {
    const payload = await response.json().catch(() => null)
    signal?.throwIfAborted()
    if (response.status === 401) window.dispatchEvent(new Event('session-expired'))
    const detail = payload?.detail
    const message = typeof detail === 'string' ? detail : Array.isArray(detail)
      ? detail.map((item: {loc?: unknown[]; msg?: string}) => `${item.loc?.slice(1).join('.') ?? ''}：${item.msg ?? '输入无效'}`).join('；')
      : '操作未完成，请稍后重试。'
    throw new ApiError(response.status, message)
  }
  return response.status === 204 ? undefined as T : response.json() as Promise<T>
}
export function projectPath(id: string): string { return `/api/projects/${encodeURIComponent(id)}` }
export function errorText(error: unknown): string {
  return error instanceof Error ? error.message === 'Failed to fetch' ? '无法连接服务，请检查网络后重试。' : error.message : '操作未完成，请重试。'
}
export function dateText(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '未设置'
}
export function localDate(value: string | null): string {
  if (!value) return ''
  const date = new Date(value)
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16)
}
export function utcDate(value: string): string | null { return value ? new Date(value).toISOString() : null }
export const stageLabels: Record<string, string> = {not_started:'未开始', planning:'方案设计中', writing:'内容输出中', checking:'内容自查中', submitted:'已提交'}
export const requestLabels: Record<string, string> = {open:'待接手',accepted:'已接手',responded:'待发起人确认',resolved:'已解决'}
export const reviewStatusLabels: Record<DeliveryReviewStatus, string> = {ai_reviewing:'AI审核中',ai_review_partial_failure:'AI审核中（部分失败）',returned:'已退回',pending_final_review:'待终审',adopted:'已采用',final_returned:'终审退回'}
export function reviewStatusText(status: DeliveryReviewStatus|null|undefined): string { return status ? reviewStatusLabels[status] : '未开始审核' }
export const reviewProgressLabels: Record<ReviewProgress['status'], string> = {queued:'等待后台处理',running:'后台处理中',retry_wait:'等待后台重试',paused:'等待审核额度',succeeded:'后台处理完成',failed:'后台处理未完成'}
export const reviewJobEventLabels: Record<string, string> = {review_report_accepted:'汇总审核报告',review_finding_annotated:'标注审核意见',review_loop_limit_reached:'审核未收敛，转交终审',delivery_final_reviewed:'记录负责人终审',review_retry_requested:'人工重试审核',review_job_queued:'审核任务已排队',review_job_claimed:'开始处理审核任务',review_job_retry_scheduled:'安排审核任务重试',review_job_paused:'审核任务等待额度',review_job_failed:'审核任务未完成',review_job_completed:'审核后台处理完成',review_job_lease_expired:'审核任务执行中断',review_job_discarded:'审核任务已停止',llm_call_reserved:'预留模型调用额度',llm_call_settled:'记录模型调用用量'}
// Historical append-only records may not have execution_origin. Only reserved IDs
// without a represented user identify the platform; agent names/metadata never do.
const platformWorkerNames: Record<string, string> = {
  'platform-review-worker': '平台审核后台',
  'platform-project-linkage-worker': '平台衔接审核后台',
  'platform-plan-self-checker': '平台方案自检',
  'platform-spec-drafter': '平台规范起草',
}
export function platformWorkerName(event: ProjectEvent): string | null {
  return event.actor_type === 'agent' && event.represented_user_id === null && Object.hasOwn(platformWorkerNames, event.actor_id) ? platformWorkerNames[event.actor_id]! : null
}
export function isPlatformReviewWorker(event: ProjectEvent): boolean { return platformWorkerName(event) !== null && event.actor_id === 'platform-review-worker' }
export const projectLinkageEventLabels: Record<string, string> = {project_linkage_requested:'发起完整衔接审核',project_linkage_claimed:'开始完整衔接审核',project_linkage_completed:'完成完整衔接审核',project_linkage_failed:'完整衔接审核未完成',project_linkage_lease_expired:'完整衔接审核执行中断',project_linkage_retry_scheduled:'安排完整衔接审核重试',project_linkage_request_created:'将衔接审核意见转为协作需求'}
export function isProjectLinkageWorker(event: ProjectEvent): boolean { return platformWorkerName(event) !== null && event.actor_id === 'platform-project-linkage-worker' }
export function sizeText(size: number): string { return size < 1024 ? `${size} B` : size < 1048576 ? `${(size/1024).toFixed(1)} KB` : `${(size/1048576).toFixed(1)} MB` }

export const planSelfCheckEventLabels: Record<string, string> = {plan_self_check_requested:'发起方案自检',plan_self_check_finished:'完成方案自检处理'}
export function isPlanSelfCheckWorker(event: ProjectEvent): boolean { return platformWorkerName(event) !== null && event.actor_id === 'platform-plan-self-checker' }
