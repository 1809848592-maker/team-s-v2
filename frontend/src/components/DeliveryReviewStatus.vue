<script setup lang="ts">
import { reviewPendingNotice, reviewStatusText } from '../api/client'
import type { DeliveryReviewStatus } from '../api/types'

defineProps<{ status: DeliveryReviewStatus|null|undefined }>()
</script>

<template>
  <div class="delivery-review-status" :data-review-status="status ?? 'not_started'">
    <p class="muted">审核状态：{{ reviewStatusText(status) }}</p>
    <p v-if="status === 'ai_reviewing'" class="muted">{{ reviewPendingNotice }}</p>
  </div>
</template>

<style scoped>
.delivery-review-status{margin:12px 0;padding-left:12px;border-left:2px solid var(--line)}.delivery-review-status p{margin:4px 0}
</style>
