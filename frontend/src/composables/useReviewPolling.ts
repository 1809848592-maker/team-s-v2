import { onBeforeUnmount, ref, watch, type Ref } from 'vue'
import { ApiError, api, errorText, projectPath } from '../api/client'
import type { Delivery } from '../api/types'

interface ReviewPollingOptions {
  contextKey: () => string
  projectId: () => string
  enabled: () => boolean
  deliveries: Ref<Delivery[]>
}

const activeStatuses = new Set(['queued', 'running', 'retry_wait'])

export function useReviewPolling(options: ReviewPollingOptions) {
  const error = ref(''), refreshing = ref(false)
  const visible = ref(document.visibilityState !== 'hidden')
  let timer: ReturnType<typeof setTimeout> | undefined
  let controller: AbortController | undefined
  let epoch = 0, mounted = true, inFlight = false, blocked = false, failures = 0
  const hasActive = () => options.deliveries.value.some(item => item.review_progress && activeStatuses.has(item.review_progress.status))
  const enabled = () => mounted && visible.value && options.enabled() && !!options.projectId()
  const canPoll = () => enabled() && hasActive() && !blocked
  function clearTimer() { if (timer !== undefined) clearTimeout(timer); timer = undefined }
  function schedule(delay = 3000) {
    clearTimer()
    if (canPoll()) timer = setTimeout(() => void poll(), delay)
  }
  async function poll(manual = false) {
    clearTimer()
    if (!enabled() || (!manual && !canPoll())) return
    if (inFlight) { schedule(); return }
    const owner = epoch, projectId = options.projectId(), key = options.contextKey()
    const current = () => enabled() && owner === epoch && projectId === options.projectId() && key === options.contextKey()
    const requestController = new AbortController()
    controller = requestController
    let timedOut = false
    const deadline = setTimeout(() => { timedOut = true; requestController.abort() }, 15000)
    inFlight = true; refreshing.value = true
    try {
      const result = await api<Delivery[]>(`${projectPath(projectId)}/deliveries`, undefined, undefined, requestController.signal)
      if (!current()) return
      const byId = new Map(result.map(item => [item.id, item]))
      // Polling must never replace forms, file selections, or the delivery collection.
      options.deliveries.value = options.deliveries.value.map(item => {
        const next = byId.get(item.id)
        return next ? { ...item, review_status: next.review_status, review_progress: next.review_progress } : item
      })
      error.value = ''; failures = 0; blocked = false
    } catch (cause) {
      if (!current()) return
      error.value = timedOut ? '读取审核进度超时，请重试。' : errorText(cause); ++failures
      if (cause instanceof ApiError && [401, 403, 404].includes(cause.status)) blocked = true
    } finally {
      clearTimeout(deadline)
      if (controller === requestController) controller = undefined
      inFlight = false; refreshing.value = false
      schedule(Math.min(3000 * Math.max(failures, 1), 15000))
    }
  }
  function retry() { blocked = false; void poll(true) }
  function onVisibilityChange() { visible.value = document.visibilityState !== 'hidden' }
  document.addEventListener('visibilitychange', onVisibilityChange)
  watch([options.contextKey, enabled, hasActive], () => {
    ++epoch; clearTimer(); controller?.abort(); error.value = ''; failures = 0; blocked = false
    schedule()
  }, { immediate: true, flush: 'sync' })
  onBeforeUnmount(() => {
    mounted = false; ++epoch; clearTimer(); controller?.abort()
    document.removeEventListener('visibilitychange', onVisibilityChange)
  })
  return { error, refreshing, retry }
}
