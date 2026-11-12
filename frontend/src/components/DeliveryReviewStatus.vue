<script setup lang="ts">
import { dateText, reviewProgressLabels, reviewStatusText } from '../api/client'
import type { DeliveryReviewStatus, ReviewProgress } from '../api/types'

defineProps<{ status: DeliveryReviewStatus|null|undefined; progress?: ReviewProgress|null; hideStatus?: boolean }>()
</script>

<template>
  <div class="delivery-review-status" :data-review-status="status ?? 'not_started'">
    <p v-if="!hideStatus" class="muted">审核状态：{{ reviewStatusText(status) }}</p>
    <template v-if="progress">
      <p class="muted" :data-review-job-status="progress.status">{{ reviewProgressLabels[progress.status] }} · 尝试次数：{{ progress.attempt }} / {{ progress.max_attempts }}</p>
      <p v-if="progress.message" class="muted">{{ progress.message }}</p>
      <p v-if="progress.status === 'retry_wait' && progress.next_attempt_at" class="muted">下次尝试：{{ dateText(progress.next_attempt_at) }}</p>
      <p v-if="progress.status === 'paused' && progress.next_attempt_at" class="muted">可再次尝试时间：{{ dateText(progress.next_attempt_at) }}</p>
      <p v-if="progress.status === 'succeeded' && status && ['returned', 'pending_final_review', 'adopted', 'final_returned'].includes(status)" class="muted">本轮后台审核已完成，请查看逐条审核意见和覆盖范围。</p>
      <p v-else-if="progress.status === 'succeeded'" class="muted">后台处理完成，正在等待审核汇总。</p>
      <p v-else-if="progress.status === 'failed'" class="muted">审核尚未完成，未生成通过结论。</p>
    </template>
    <p v-else-if="status === 'ai_reviewing' || status === 'ai_review_partial_failure'" class="muted">暂未读取到后台审核进度。</p>
  </div>
</template>

<style scoped>
.delivery-review-status{margin:12px 0;padding-left:12px;border-left:2px solid var(--line)}.delivery-review-status p{margin:4px 0}
</style>
