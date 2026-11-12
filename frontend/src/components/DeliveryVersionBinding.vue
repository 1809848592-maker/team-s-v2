<script setup lang="ts">
import { computed } from 'vue'
import { dateText } from '../api/client'
import type { Delivery, Member } from '../api/types'

const props = defineProps<{ delivery: Delivery; members?: Member[]; disabled?: boolean; requesting?: boolean; compact?: boolean }>()
defineEmits<{ request: [] }>()
const binding = computed(() => props.delivery.version_binding)
const request = computed(() => binding.value?.resubmission_request)
const requestedBy = computed(() => props.members?.find(member => member.user_id === request.value?.requested_by)?.username ?? request.value?.requested_by)
const requestedCurrentVersions = computed(() => !!request.value && request.value.target_plan_version_id === binding.value?.latest_plan_version_id && request.value.target_spec_version_id === binding.value?.latest_spec_version_id)
</script>

<template>
  <div v-if="binding" class="version-binding" :class="{ compact }" :data-delivery-binding="delivery.id">
    <div v-if="binding.based_on_old_version" class="binding-flags">
      <span v-if="binding.plan_outdated" class="tag old-version">依据旧版方案</span>
      <span v-if="binding.spec_outdated" class="tag old-version">依据旧版规范</span>
    </div>
    <template v-if="!compact">
      <p class="muted">提交依据：方案 v{{ binding.bound_plan_version }} · 规范 v{{ binding.bound_spec_version }}<span v-if="binding.plan_outdated || binding.spec_outdated">；当前最新：方案 v{{ binding.latest_plan_version }} · 规范 v{{ binding.latest_spec_version }}</span></p>
      <p v-if="binding.based_on_old_version" class="muted">此版本保持采用。重新提交会生成绑定最新方案和规范的新交付版本。</p>
      <p v-else-if="(binding.plan_outdated || binding.spec_outdated) && ['ai_reviewing', 'ai_review_partial_failure', 'pending_final_review'].includes(delivery.review_status ?? '')" class="muted">本次审核继续使用提交时绑定的方案和规范。</p>
      <p v-if="request" class="resubmission-note"><strong>{{ requestedCurrentVersions ? '已要求按当前版本重新提交' : '此前已要求重新提交' }}</strong><span>{{ requestedBy }} · {{ dateText(request.requested_at) }}</span></p>
      <button v-if="binding.can_request_resubmission" type="button" class="btn" :disabled="disabled || requesting" :data-resubmission-request="delivery.id" @click="$emit('request')">{{ requesting ? '正在发送…' : '要求重新提交' }}</button>
    </template>
    <p v-else-if="request" class="muted compact-request">已要求重新提交</p>
  </div>
</template>

<style scoped>
.version-binding{margin:14px 0;overflow-wrap:anywhere}.binding-flags{display:flex;flex-wrap:wrap;gap:8px}.old-version{background:#fff2d5;color:#71531d;border:1px solid #e8d29e}.version-binding p{font-size:13px;line-height:1.7}.resubmission-note{padding-left:12px;border-left:2px solid var(--teal);display:flex;flex-direction:column;gap:4px}.resubmission-note span{font-size:12px;color:var(--muted)}.compact{margin:8px 0}.compact-request{margin:8px 0 0}
</style>
