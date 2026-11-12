import { onBeforeUnmount, ref, watch } from 'vue'
import { ApiError, api, errorText, projectPath } from '../api/client'
import type { ProjectBottlenecks } from '../api/types'

interface Options {
  contextKey: () => string
  projectId: () => string
  enabled: () => boolean
}

// 时间经过本身也会产生卡点，不能依赖“正在审核”才刷新。
const refreshMs = 60000, timeoutMs = 15000

export function useBottlenecks(options: Options) {
  const data = ref<ProjectBottlenecks | null>(null), error = ref(''), refreshing = ref(false)
  const visible = ref(document.visibilityState !== 'hidden')
  let mounted = true, epoch = 0, blocked = false
  let timer: ReturnType<typeof setTimeout> | undefined
  let active: { controller: AbortController; deadline: ReturnType<typeof setTimeout> } | undefined
  const enabled = () => mounted && visible.value && options.enabled() && !!options.projectId()
  function clearTimer() { if (timer !== undefined) clearTimeout(timer); timer = undefined }
  function cancel() {
    clearTimer()
    if (active) { clearTimeout(active.deadline); active.controller.abort(); active = undefined }
    refreshing.value = false
  }
  async function refresh() {
    if (!enabled() || active) return
    clearTimer()
    blocked = false
    const owner = epoch, key = options.contextKey(), projectId = options.projectId()
    const current = () => enabled() && owner === epoch && key === options.contextKey() && projectId === options.projectId()
    let timedOut = false
    const controller = new AbortController()
    const request = { controller, deadline: setTimeout(() => { timedOut = true; controller.abort() }, timeoutMs) }
    active = request; refreshing.value = true
    try {
      const result = await api<ProjectBottlenecks>(`${projectPath(projectId)}/bottlenecks`, undefined, undefined, controller.signal)
      if (!current()) return
      controller.signal.throwIfAborted()
      if (result.project_id !== projectId) throw new Error('卡点记录与当前项目不一致，请重新读取。')
      data.value = result; error.value = ''
    } catch (cause) {
      if (!current()) return
      data.value = null
      error.value = timedOut ? '读取卡点超时，请重试。' : errorText(cause)
      blocked = cause instanceof ApiError && [401, 403, 404].includes(cause.status)
    } finally {
      clearTimeout(request.deadline)
      if (active === request) {
        active = undefined; refreshing.value = false
        if (current() && !blocked) timer = setTimeout(() => void refresh(), refreshMs)
      }
    }
  }
  function onVisibilityChange() { visible.value = document.visibilityState !== 'hidden' }
  document.addEventListener('visibilitychange', onVisibilityChange)
  watch([options.contextKey, enabled], () => {
    ++epoch; cancel(); data.value = null; error.value = ''; blocked = false
    if (enabled()) void refresh()
  }, { immediate: true, flush: 'sync' })
  onBeforeUnmount(() => {
    mounted = false; ++epoch; cancel()
    document.removeEventListener('visibilitychange', onVisibilityChange)
  })
  return { data, error, refreshing, refresh }
}
