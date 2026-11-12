<script setup lang="ts">
import type { ModelDisclosure } from '../api/types'
defineProps<{ notice: ModelDisclosure; compact?: boolean }>()
</script>

<template>
  <div class="model-disclosure" :class="{ compact }" data-model-disclosure>
    <strong>审核数据外发 · {{ notice.provider }}</strong>
    <p>{{ notice.summary }}</p>
    <p>{{ notice.uncertainty }}</p>
    <div class="disclosure-links"><a :href="notice.terms_url" target="_blank" rel="noopener noreferrer">开放平台服务协议</a><a :href="notice.privacy_url" target="_blank" rel="noopener noreferrer">隐私政策</a></div>
  </div>
</template>

<style scoped>
.model-disclosure{padding:18px;background:var(--canvas);border:1px solid var(--line);border-left:3px solid var(--teal);border-radius:5px;font-size:13px;line-height:1.7}.model-disclosure p{margin:10px 0}.disclosure-links{display:flex;gap:16px;flex-wrap:wrap}.compact{padding:12px;font-size:12px}.compact .disclosure-links{gap:6px 12px}
</style>
