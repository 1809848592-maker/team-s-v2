<script setup lang="ts">
import { computed } from 'vue'
import type { ProjectLinkageFinding, ProjectLinkageResult } from '../api/types'

const props = defineProps<{ result: ProjectLinkageResult; canConvert: boolean; disabled?: boolean }>()
defineEmits<{ convert: [finding: ProjectLinkageFinding]; openRequest: [requestId: string] }>()
const covered = computed(() => props.result.modules.filter(module => module.state === 'summarized').length)
const checkNames: Record<string, string> = { connection: '上下游衔接', overlap: '重复内容', omission: '内容遗漏', contradiction: '矛盾', plan_alignment: '方案执行' }
function moduleName(id: string) { return props.result.modules.find(module => module.module_id === id)?.module_name ?? `历史模块 ${id}` }
function sourceLocation(location: Record<string, unknown>) {
  const part = typeof location.part === 'string' ? location.part : ''
  const parts = [part === 'handoff_note' ? '交接说明' : part === 'markdown' ? '文稿' : part === 'pdf' ? '文档' : part === 'word/document.xml' ? '正文' : /^word\/header\d+\.xml$/.test(part) ? '页眉' : /^word\/footer\d+\.xml$/.test(part) ? '页脚' : part === 'word/footnotes.xml' ? '脚注' : part === 'word/endnotes.xml' ? '尾注' : /^ppt\/slides\/slide\d+\.xml$/.test(part) ? '幻灯片' : '原文内容']
  const number = (key: string) => typeof location[key] === 'number' && Number.isSafeInteger(location[key]) && location[key] > 0 ? location[key] as number : undefined
  for (const [key, unit] of [['page', '页'], ['slide', '张'], ['paragraph', '段'], ['table', '个表'], ['row', '行'], ['column', '列']]) {
    const value = number(key)
    if (value !== undefined) parts.push(`第${value}${unit}`)
  }
  const start = number('line_start'), end = number('line_end')
  if (start !== undefined) parts.push(`第${start}${end !== undefined && end > start ? `至${end}` : ''}行`)
  if (typeof location.shape_id === 'string' && location.shape_id) parts.push(`图形${location.shape_id}`)
  return parts.join(' · ')
}
</script>

<template>
  <div class="linkage-report">
    <div class="report-summary" :data-coverage="result.coverage"><strong>{{ result.coverage === 'completed' ? '审核流程已完成' : '审核存在未完成或未覆盖范围' }}</strong><span>{{ covered }} / {{ result.modules.length }} 个模块已生成摘要</span><span>{{ result.findings.length }} 条审核意见</span></div>
    <p class="muted">AI 生成，供参考。报告用于分派后续协作，不改变交付的采用状态。</p>
    <p v-if="result.coverage === 'incomplete'" class="coverage-warning" role="status">未完成、未覆盖的范围不作通过判断；请结合下方范围说明查看报告。</p>
    <section aria-labelledby="linkage-sources-title" class="report-section"><h4 id="linkage-sources-title">本次审核范围与模块摘要</h4>
      <p class="muted">报告使用触发时冻结的方案与交付版本。新交付或方案更新不会替换这份历史报告。</p>
      <p v-if="!result.modules.length" class="coverage-warning">本次没有可审核的模块，未覆盖。</p>
      <details v-for="module in result.modules" :key="module.module_id" class="module-summary" :data-module-state="module.state">
        <summary><strong>{{ module.module_name }}</strong><span class="tag">{{ module.state === 'summarized' ? '摘要已完成' : module.state === 'uncovered' ? '未覆盖' : '未完成' }}</span><span v-if="module.delivery_version !== null" class="muted">已采用交付 v{{ module.delivery_version }}</span></summary>
        <p v-if="module.plan_outdated || module.spec_outdated" class="coverage-warning" data-old-binding>交付依据旧版{{ [module.plan_outdated ? '方案' : '', module.spec_outdated ? '规范' : ''].filter(Boolean).join(' / ') }}；审核按本次冻结的项目方案比对。</p>
        <p v-if="module.reason" class="pre-wrap">{{ module.reason }}</p><p v-if="module.summary" class="pre-wrap">{{ module.summary }}</p>
        <details v-if="module.source_segments?.length" class="segment-index"><summary>摘要中片段编号对应的原文位置</summary><p class="muted">用于查找原文；摘要中的引用仍需人工对照。</p><ul><li v-for="segment in module.source_segments" :key="segment.segment_id">{{ segment.segment_id }} · {{ sourceLocation(segment.location) }}</li></ul></details>
        <p v-if="module.delivery_id" class="source-id">交付 {{ module.delivery_id }}<template v-if="module.plan_version_id"> · 依据方案 {{ module.plan_version_id }}</template><template v-if="module.spec_version_id"> · 依据规范 {{ module.spec_version_id }}</template></p>
        <p v-if="module.provenance" class="source-id">{{ module.provenance.reviewer }} · 指令 {{ module.provenance.instruction_version }} · {{ module.provenance.model }} · 输入 {{ module.provenance.input_hash }}</p>
      </details>
    </section>
    <section class="report-section" aria-labelledby="linkage-coverage-title"><h4 id="linkage-coverage-title">整体比对与覆盖说明</h4><p>整体比对：{{ result.comparison.state === 'completed' ? '已完成' : result.comparison.state === 'uncovered' ? '未覆盖' : '未完成' }}</p><dl v-if="result.comparison.checks?.length" class="comparison-checks"><template v-for="check in result.comparison.checks" :key="check.key"><dt>{{ checkNames[check.key] ?? check.key }}</dt><dd><strong>{{ check.status === 'issues' ? '有审核意见' : check.status === 'checked' ? '已核对摘要' : '未覆盖' }}</strong><span>{{ check.reason }}</span></dd></template></dl><ul v-if="result.coverage_notes.length" class="coverage-notes"><li v-for="(note,index) in result.coverage_notes" :key="index">{{ note }}</li></ul><p v-else class="muted">没有额外覆盖说明。</p><p v-if="result.discarded_findings" class="coverage-warning">{{ result.discarded_findings }} 条意见因格式无效未采用。</p></section>
    <section class="report-section" aria-labelledby="linkage-findings-title"><h4 id="linkage-findings-title">审核意见</h4>
      <p v-if="!result.findings.length" class="empty">{{ result.coverage === 'completed' ? '本次已覆盖范围未提出审核意见，仍需负责人核对。' : '当前没有有效审核意见；未完成或未覆盖范围仍需处理。' }}</p>
      <article v-for="finding in result.findings" :key="finding.id" class="linkage-finding" :data-finding-id="finding.id">
        <div class="toolbar"><span class="tag">{{ checkNames[finding.check_key] ?? finding.check_key }}</span><span class="tag" :class="{ important: finding.severity === 'important' }">{{ finding.severity === 'important' ? '重要' : '建议' }}</span><span class="muted">AI 生成，供参考</span></div>
        <h5>{{ finding.problem }}</h5><p class="muted">涉及模块：{{ finding.module_ids.map(moduleName).join('、') || '项目整体' }}</p>
        <dl><dt>摘要摘录</dt><dd class="pre-wrap">{{ finding.excerpt || '未提供摘录' }}</dd><dt>依据</dt><dd class="pre-wrap">{{ finding.basis }}</dd><dt>修改建议</dt><dd class="pre-wrap">{{ finding.suggestion }}</dd></dl>
        <p v-if="finding.evidence_status === 'unverified'" class="coverage-warning" data-evidence-unverified>摘要摘录未能核对，请对照摘要及已采用交付原文验证这条意见。</p><p v-else class="muted evidence-note" data-evidence-verified>摘要摘录已核对，原文仍需复核。</p>
        <details class="finding-source"><summary>审核来源</summary><p>{{ finding.reviewer }} · 指令 {{ finding.instruction_version }} · {{ finding.model }}</p><p class="source-id">输入 {{ finding.input_hash }}</p></details>
        <div class="toolbar finding-actions"><button v-if="finding.request_id" type="button" class="btn quiet" @click="$emit('openRequest', finding.request_id)">已转为协作需求 · 查看需求 →</button><button v-else-if="canConvert" type="button" class="btn" :disabled="disabled" @click="$emit('convert', finding)">转为协作需求</button></div>
      </article>
    </section>
  </div>
</template>

<style scoped>
.segment-index{margin:16px 0;font-size:12px}.segment-index>summary{cursor:pointer;color:var(--teal)}.segment-index ul{padding-left:22px;line-height:1.9;max-height:220px;overflow:auto}.comparison-checks{display:grid;grid-template-columns:100px minmax(0,1fr);gap:12px;font-size:13px;line-height:1.7}.comparison-checks dt{color:var(--muted)}.comparison-checks dd{margin:0;display:flex;gap:12px;flex-wrap:wrap}.comparison-checks dd span{color:var(--muted);overflow-wrap:anywhere}.evidence-note{font-size:12px}
.report-summary{display:flex;gap:16px;flex-wrap:wrap;padding:16px;background:var(--canvas);border-radius:6px;font-size:13px}.report-summary strong{color:var(--teal)}.report-summary[data-coverage="incomplete"] strong{color:#8a5417}.report-section{margin-top:26px;border-top:1px solid var(--line);padding-top:20px}.report-section h4{margin:0 0 16px;font-size:15px}.coverage-warning{padding:10px 12px;background:#fff8e9;color:#7a4e15;font-size:13px;border-left:3px solid #d4a75c;line-height:1.7}.module-summary{border:1px solid var(--line);padding:14px;border-radius:6px;margin-top:10px;font-size:13px}.module-summary>summary{display:flex;align-items:center;gap:12px;cursor:pointer;flex-wrap:wrap}.module-summary p{line-height:1.8}.source-id{font-size:11px;color:var(--muted);overflow-wrap:anywhere;line-height:1.6}.coverage-notes{font-size:13px;line-height:1.8;padding-left:22px}.linkage-finding{padding:20px;border:1px solid var(--line);border-radius:7px;margin-top:14px}.linkage-finding h5{font-size:15px;margin:16px 0 10px;line-height:1.7;overflow-wrap:anywhere}.linkage-finding>.muted{font-size:12px}.linkage-finding dl{display:grid;grid-template-columns:66px minmax(0,1fr);gap:12px;font-size:13px;line-height:1.8}.linkage-finding dt{color:var(--muted)}.linkage-finding dd{margin:0;overflow-wrap:anywhere}.important{color:#89520f;background:#fff2d8}.finding-source{font-size:12px;color:var(--muted);margin:16px 0}.finding-source summary{cursor:pointer}.finding-actions{margin-top:16px}
</style>
