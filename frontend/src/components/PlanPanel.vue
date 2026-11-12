<script setup lang="ts">
import { computed, onMounted, ref, useId } from 'vue'
import { api, dateText, errorText, localDate, projectPath, utcDate } from '../api/client'
import type { Member, Module, PlanContent, PlanVersion } from '../api/types'
import DependencyGraph from './DependencyGraph.vue'
import PlanSelfCheck from './PlanSelfCheck.vue'

const props = defineProps<{ projectId: string; accountId: string; members: Member[]; leader: boolean }>()
const emit = defineEmits<{ changed: [] }>()
const prefix = `plan-${useId().replace(/:/g, '')}`
const versions = ref<PlanVersion[]>([])
const selectedId = ref('')
const selected = computed(() => versions.value.find(version => version.id === selectedId.value))
const draft = ref<PlanContent | null>(null)
const editing = ref(false)
const changeNote = ref('')
const loading = ref(true)
const loaded = ref(false)
const publishing = ref(false)
const error = ref('')
const notice = ref('')
const formats: { value: Module['delivery_format']; label: string }[] = [
  { value: 'docx', label: 'Word 文稿（docx）' }, { value: 'pptx', label: '幻灯片（pptx）' },
  { value: 'pdf', label: 'PDF' }, { value: 'md', label: 'Markdown（md）' }, { value: 'other', label: '其他' },
]
const graphModules = computed(() => editing.value && draft.value ? draft.value.modules : selected.value?.content.modules ?? [])
const selfCheckContent = computed(() => {
  if (!draft.value) return null
  const content = copyContent(draft.value)
  content.deadline = utcDate(content.deadline ?? '')
  content.modules.forEach(module => { module.deadline = utcDate(module.deadline ?? ''); delete module.plan_version_id })
  return content
})

function copyContent(content: PlanContent): PlanContent { return JSON.parse(JSON.stringify(content)) as PlanContent }
function memberName(id: string): string { return props.members.find(member => member.user_id === id)?.username ?? id }
function ownerNames(module: Module): string { return module.assignee_ids.map(memberName).join('、') || '未分配' }
function moduleName(id: string, modules: Module[]): string { return modules.find(module => module.id === id)?.name || '未命名模块' }
function formatLabel(value: Module['delivery_format']): string { return formats.find(format => format.value === value)?.label ?? value }
function noteLabel(value: string): string { return value.length > 35 ? `${value.slice(0, 35)}…` : value }

async function loadVersions(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    versions.value = await api<PlanVersion[]>(`${projectPath(props.projectId)}/plan/versions`)
    if (!versions.value.some(version => version.id === selectedId.value)) selectedId.value = versions.value[0]?.id ?? ''
    loaded.value = true
  } catch (cause) { error.value = errorText(cause) }
  finally { loading.value = false }
}

function editLatest(): void {
  if (!props.leader || !loaded.value) return
  if (!draft.value) {
    const latest = versions.value[0]
    const content = latest ? copyContent(latest.content) : {
      research_question: '', final_deliverable: '', deadline: null, argument: '', terms: [], data_definitions: [], modules: [],
    }
    content.deadline = localDate(content.deadline) || null
    content.modules.forEach(module => { module.deadline = localDate(module.deadline) || null })
    draft.value = content
    changeNote.value = ''
  }
  editing.value = true
  error.value = ''
  notice.value = ''
}

function showVersion(): void { editing.value = false; error.value = ''; notice.value = '' }

function addModule(): void {
  if (!draft.value || draft.value.modules.length >= 200) return
  draft.value.modules.push({
    id: crypto.randomUUID(), name: '', assignee_ids: [], deadline: null, goal: '', core_argument: '',
    scope_in: '', scope_out: '', upstream: [], downstream_handoff: '', delivery_format: 'docx',
  })
}

function removeModule(id: string): void {
  if (!draft.value) return
  draft.value.modules = draft.value.modules.filter(module => module.id !== id)
  draft.value.modules.forEach(module => { module.upstream = module.upstream.filter(dependency => dependency.module_id !== id) })
  notice.value = '已从草稿移除模块及指向它的依赖。'
}

function dependencyOptions(module: Module, selectedModuleId?: string): Module[] {
  return draft.value?.modules.filter(candidate => candidate.id !== module.id
    && (candidate.id === selectedModuleId || !module.upstream.some(dependency => dependency.module_id === candidate.id))) ?? []
}

function addDependency(module: Module): void {
  const upstream = dependencyOptions(module)[0]
  if (upstream) module.upstream.push({ module_id: upstream.id, needs: '' })
}

function validateDependencies(modules: Module[]): void {
  const byId = new Map(modules.map(module => [module.id, module]))
  const visiting = new Set<string>()
  const visited = new Set<string>()
  function visit(id: string): void {
    if (visiting.has(id)) throw new Error('上游依赖不能形成环，请调整依赖后再发布。')
    if (visited.has(id)) return
    visiting.add(id)
    for (const dependency of byId.get(id)?.upstream ?? []) {
      if (!byId.has(dependency.module_id)) throw new Error('上游依赖必须指向本次方案中的模块。')
      visit(dependency.module_id)
    }
    visiting.delete(id)
    visited.add(id)
  }
  modules.forEach(module => visit(module.id))
}

async function publish(): Promise<void> {
  if (!draft.value || publishing.value || !props.leader) return
  error.value = ''
  notice.value = ''
  publishing.value = true
  try {
    const content = copyContent(draft.value)
    validateDependencies(content.modules)
    content.deadline = utcDate(content.deadline ?? '')
    content.modules.forEach(module => { module.deadline = utcDate(module.deadline ?? ''); delete module.plan_version_id })
    const version = await api<PlanVersion>(`${projectPath(props.projectId)}/plan/versions`, { change_note: changeNote.value, content })
    versions.value = [version, ...versions.value]
    selectedId.value = version.id
    editing.value = false
    draft.value = null
    changeNote.value = ''
    notice.value = `方案第 ${version.version} 版已发布。`
    emit('changed')
  } catch (cause) { error.value = errorText(cause) }
  finally { publishing.value = false }
}

onMounted(loadVersions)
</script>

<template>
  <section class="panel plan-panel" aria-labelledby="plan-heading">
    <header class="panel-heading">
      <div><h2 id="plan-heading">方案与分工</h2><p class="muted">明确共同目标、内容边界，以及每个模块需要交给谁什么。</p></div>
      <button v-if="leader && !editing" class="btn primary" type="button" :disabled="loading || !loaded" @click="editLatest">{{ draft ? '继续编辑草稿' : versions.length ? '基于最新版本编辑' : '创建方案' }}</button>
      <button v-if="editing" class="btn quiet" type="button" :disabled="publishing" @click="showVersion">查看已发布方案</button>
    </header>

    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="notice" class="notice" role="status">{{ notice }}</p>
    <p v-if="loading" class="muted" role="status">正在读取方案版本…</p>
    <div v-if="error && !loaded" class="toolbar"><button type="button" class="btn quiet" @click="loadVersions">重新加载</button></div>

    <div v-if="versions.length" class="toolbar version-toolbar">
      <div class="field"><label :for="`${prefix}-version`">查看已发布版本</label>
        <select :id="`${prefix}-version`" v-model="selectedId" :disabled="publishing" @change="showVersion">
          <option v-for="version in versions" :key="version.id" :value="version.id">第 {{ version.version }} 版{{ version.id === versions[0]?.id ? '（最新）' : '' }} · {{ noteLabel(version.change_note) }}</option>
        </select>
      </div>
      <span v-if="draft && !editing" class="muted">未发布草稿已保留，可继续编辑。</span>
    </div>

    <form v-if="editing && draft && leader" class="stack plan-editor" @submit.prevent="publish">
      <p class="notice">草稿尚未发布。查看历史版本会保留草稿；离开此页面前请完成发布。</p>
      <PlanSelfCheck v-if="selfCheckContent" :project-id="projectId" :account-id="accountId" :leader="leader" :content="selfCheckContent" />
      <fieldset :disabled="publishing">
        <legend>整体方案</legend>
        <div class="form-grid">
          <div class="field wide-field"><label :for="`${prefix}-question`">研究问题 / 作业目标 <span aria-hidden="true">*</span></label><textarea :id="`${prefix}-question`" v-model="draft.research_question" required maxlength="10000" rows="3" /></div>
          <div class="field"><label :for="`${prefix}-deliverable`">最终交付物 <span aria-hidden="true">*</span></label><input :id="`${prefix}-deliverable`" v-model="draft.final_deliverable" required maxlength="2000" placeholder="例如：研究论文、课题报告或汇报 PPT" /></div>
          <div class="field"><label :for="`${prefix}-deadline`">总截止时间（本地时间）</label><input :id="`${prefix}-deadline`" v-model="draft.deadline" type="datetime-local" /></div>
          <div class="field wide-field"><label :for="`${prefix}-argument`">整体论证主线</label><textarea :id="`${prefix}-argument`" v-model="draft.argument" maxlength="50000" rows="4" /></div>
        </div>
      </fieldset>

      <fieldset :disabled="publishing">
        <legend>关键术语与定义</legend>
        <p class="muted">统一各模块使用的词语、定义及出处。</p>
        <p v-if="!draft.terms.length" class="empty">尚未添加术语。</p>
        <fieldset v-for="(term, index) in draft.terms" :key="index" class="entry-fieldset">
          <legend>术语 {{ index + 1 }}</legend>
          <div class="form-grid">
            <div class="field"><label :for="`${prefix}-term-${index}`">术语 <span aria-hidden="true">*</span></label><input :id="`${prefix}-term-${index}`" v-model="term.term" required maxlength="120" /></div>
            <div class="field"><label :for="`${prefix}-term-source-${index}`">出处</label><input :id="`${prefix}-term-source-${index}`" v-model="term.source" maxlength="2000" /></div>
            <div class="field wide-field"><label :for="`${prefix}-definition-${index}`">定义 <span aria-hidden="true">*</span></label><textarea :id="`${prefix}-definition-${index}`" v-model="term.definition" required maxlength="5000" rows="2" /></div>
          </div>
          <button type="button" class="btn danger" :aria-label="`删除术语 ${index + 1}`" @click="draft.terms.splice(index, 1)">删除术语</button>
        </fieldset>
        <button type="button" class="btn quiet" :disabled="draft.terms.length >= 200" @click="draft.terms.push({term:'',definition:'',source:''})">添加术语</button>
      </fieldset>

      <fieldset :disabled="publishing">
        <legend>数据口径</legend>
        <p class="muted">记录变量、来源和处理方式，方便下游使用同一口径。</p>
        <p v-if="!draft.data_definitions.length" class="empty">尚未添加数据口径。</p>
        <fieldset v-for="(data, index) in draft.data_definitions" :key="index" class="entry-fieldset">
          <legend>数据口径 {{ index + 1 }}</legend>
          <div class="form-grid">
            <div class="field"><label :for="`${prefix}-variable-${index}`">变量 <span aria-hidden="true">*</span></label><input :id="`${prefix}-variable-${index}`" v-model="data.variable" required maxlength="120" /></div>
            <div class="field"><label :for="`${prefix}-data-source-${index}`">来源</label><input :id="`${prefix}-data-source-${index}`" v-model="data.source" maxlength="2000" /></div>
            <div class="field wide-field"><label :for="`${prefix}-processing-${index}`">处理方式</label><textarea :id="`${prefix}-processing-${index}`" v-model="data.processing" maxlength="5000" rows="2" /></div>
          </div>
          <button type="button" class="btn danger" :aria-label="`删除数据口径 ${index + 1}`" @click="draft.data_definitions.splice(index, 1)">删除数据口径</button>
        </fieldset>
        <button type="button" class="btn quiet" :disabled="draft.data_definitions.length >= 200" @click="draft.data_definitions.push({variable:'',source:'',processing:''})">添加数据口径</button>
      </fieldset>

      <fieldset :disabled="publishing">
        <legend>分工模块</legend>
        <p class="muted">每个模块由一名或多名项目成员共同负责；统稿和 PPT 整合也可以单列为模块。</p>
        <p v-if="!draft.modules.length" class="empty">尚未添加分工模块。</p>
        <fieldset v-for="(module, index) in draft.modules" :key="module.id" class="entry-fieldset module-editor">
          <legend>模块 {{ index + 1 }}{{ module.name ? ` · ${module.name}` : '' }}</legend>
          <div class="form-grid">
            <div class="field"><label :for="`${prefix}-name-${module.id}`">模块名称 <span aria-hidden="true">*</span></label><input :id="`${prefix}-name-${module.id}`" v-model="module.name" required maxlength="120" /></div>
            <div class="field"><label :for="`${prefix}-module-deadline-${module.id}`">模块截止时间（本地时间）</label><input :id="`${prefix}-module-deadline-${module.id}`" v-model="module.deadline" type="datetime-local" /></div>
            <div class="field"><label :for="`${prefix}-owners-${module.id}`">模块负责人，可多选 <span aria-hidden="true">*</span></label>
              <select :id="`${prefix}-owners-${module.id}`" v-model="module.assignee_ids" multiple required :size="Math.min(Math.max(members.length, 2), 5)" :aria-describedby="`${prefix}-owner-help-${module.id}`">
                <option v-for="member in members" :key="member.user_id" :value="member.user_id">{{ member.username }}{{ member.role === 'leader' ? '（项目负责人）' : '' }}</option>
              </select>
              <span :id="`${prefix}-owner-help-${module.id}`" class="muted">按住 Ctrl 或 ⌘ 可选择多名负责人。请先邀请成员入组。</span>
            </div>
            <div class="field"><label :for="`${prefix}-format-${module.id}`">交付形式 <span aria-hidden="true">*</span></label><select :id="`${prefix}-format-${module.id}`" v-model="module.delivery_format" required><option v-for="format in formats" :key="format.value" :value="format.value">{{ format.label }}</option></select></div>
            <div class="field wide-field"><label :for="`${prefix}-goal-${module.id}`">模块目标：要回答的问题 <span aria-hidden="true">*</span></label><textarea :id="`${prefix}-goal-${module.id}`" v-model="module.goal" required maxlength="10000" rows="3" /></div>
            <div class="field wide-field"><label :for="`${prefix}-core-${module.id}`">核心论点 / 预期结论</label><textarea :id="`${prefix}-core-${module.id}`" v-model="module.core_argument" maxlength="10000" rows="2" /></div>
            <div class="field"><label :for="`${prefix}-scope-in-${module.id}`">内容边界：写什么</label><textarea :id="`${prefix}-scope-in-${module.id}`" v-model="module.scope_in" maxlength="10000" rows="3" /></div>
            <div class="field"><label :for="`${prefix}-scope-out-${module.id}`">内容边界：不写什么</label><textarea :id="`${prefix}-scope-out-${module.id}`" v-model="module.scope_out" maxlength="10000" rows="3" /></div>
            <div class="field wide-field"><label :for="`${prefix}-handoff-${module.id}`">要交给下游什么</label><textarea :id="`${prefix}-handoff-${module.id}`" v-model="module.downstream_handoff" maxlength="10000" rows="3" /></div>
          </div>
          <fieldset class="dependency-editor">
            <legend>需要上游提供什么</legend>
            <p v-if="!module.upstream.length" class="muted">尚未设置上游依赖。</p>
            <fieldset v-for="(dependency, dependencyIndex) in module.upstream" :key="dependencyIndex" class="entry-fieldset">
              <legend>上游依赖 {{ dependencyIndex + 1 }}</legend>
              <div class="field"><label :for="`${prefix}-upstream-${module.id}-${dependencyIndex}`">上游模块 <span aria-hidden="true">*</span></label><select :id="`${prefix}-upstream-${module.id}-${dependencyIndex}`" v-model="dependency.module_id" required><option v-for="candidate in dependencyOptions(module, dependency.module_id)" :key="candidate.id" :value="candidate.id">{{ candidate.name || '未命名模块' }}</option></select></div>
              <div class="field"><label :for="`${prefix}-needs-${module.id}-${dependencyIndex}`">需要上游提供的内容 <span aria-hidden="true">*</span></label><textarea :id="`${prefix}-needs-${module.id}-${dependencyIndex}`" v-model="dependency.needs" required maxlength="5000" rows="2" /></div>
              <button type="button" class="btn danger" :aria-label="`删除模块 ${index + 1} 的上游依赖 ${dependencyIndex + 1}`" @click="module.upstream.splice(dependencyIndex, 1)">删除依赖</button>
            </fieldset>
            <button type="button" class="btn quiet" :disabled="!dependencyOptions(module).length" @click="addDependency(module)">添加上游依赖</button>
          </fieldset>
          <button type="button" class="btn danger" :aria-label="`删除模块 ${index + 1}`" @click="removeModule(module.id)">删除模块</button>
        </fieldset>
        <button type="button" class="btn quiet" :disabled="draft.modules.length >= 200" @click="addModule">添加分工模块</button>
      </fieldset>

      <fieldset :disabled="publishing">
        <legend>发布新版本</legend>
        <div class="field"><label :for="`${prefix}-change-note`">变更说明 <span aria-hidden="true">*</span></label><textarea :id="`${prefix}-change-note`" v-model="changeNote" required maxlength="2000" rows="2" placeholder="说明这次发布明确或调整了什么" /></div>
        <p class="muted">发布后生成只读版本，并通知受影响模块的负责人。</p>
        <button class="btn primary" type="submit">{{ publishing ? '正在发布…' : '发布方案新版本' }}</button>
      </fieldset>
    </form>

    <div v-else-if="selected" class="stack plan-reading">
      <div class="version-meta"><span class="tag">第 {{ selected.version }} 版{{ selected.id === versions[0]?.id ? ' · 最新' : ' · 历史' }}</span><span class="muted">{{ memberName(selected.published_by) }} 发布于 {{ dateText(selected.published_at) }}</span></div>
      <p class="change-note"><strong>变更说明</strong>{{ selected.change_note }}</p>
      <section aria-label="整体方案内容">
        <h3>整体方案</h3>
        <dl class="read-grid">
          <div class="wide-field"><dt>研究问题 / 作业目标</dt><dd>{{ selected.content.research_question }}</dd></div>
          <div><dt>最终交付物</dt><dd>{{ selected.content.final_deliverable }}</dd></div>
          <div><dt>总截止时间</dt><dd>{{ dateText(selected.content.deadline) }}</dd></div>
          <div class="wide-field"><dt>整体论证主线</dt><dd>{{ selected.content.argument || '尚未填写' }}</dd></div>
        </dl>
      </section>
      <section aria-label="术语表">
        <h3>关键术语与定义</h3>
        <div v-if="selected.content.terms.length" class="table-wrap"><table><thead><tr><th scope="col">术语</th><th scope="col">定义</th><th scope="col">出处</th></tr></thead><tbody><tr v-for="(term, index) in selected.content.terms" :key="index"><th scope="row">{{ term.term }}</th><td>{{ term.definition }}</td><td>{{ term.source || '未填写' }}</td></tr></tbody></table></div>
        <p v-else class="empty">本版未设置术语。</p>
      </section>
      <section aria-label="数据口径表">
        <h3>数据口径</h3>
        <div v-if="selected.content.data_definitions.length" class="table-wrap"><table><thead><tr><th scope="col">变量</th><th scope="col">来源</th><th scope="col">处理方式</th></tr></thead><tbody><tr v-for="(data, index) in selected.content.data_definitions" :key="index"><th scope="row">{{ data.variable }}</th><td>{{ data.source || '未填写' }}</td><td>{{ data.processing || '未填写' }}</td></tr></tbody></table></div>
        <p v-else class="empty">本版未设置数据口径。</p>
      </section>
      <section class="stack" aria-label="分工模块详情">
        <h3>分工模块 <span class="muted">{{ selected.content.modules.length }} 个</span></h3>
        <p v-if="!selected.content.modules.length" class="empty">本版未设置分工模块。</p>
        <article v-for="module in selected.content.modules" :key="module.id" class="record">
          <h4>{{ module.name }}</h4>
          <p class="muted">{{ ownerNames(module) }} · 截止 {{ dateText(module.deadline) }} · {{ formatLabel(module.delivery_format) }}</p>
          <dl class="read-grid">
            <div class="wide-field"><dt>模块目标</dt><dd>{{ module.goal }}</dd></div>
            <div class="wide-field"><dt>核心论点 / 预期结论</dt><dd>{{ module.core_argument || '尚未填写' }}</dd></div>
            <div><dt>写什么</dt><dd>{{ module.scope_in || '尚未填写' }}</dd></div>
            <div><dt>不写什么</dt><dd>{{ module.scope_out || '尚未填写' }}</dd></div>
            <div class="wide-field"><dt>需要上游提供什么</dt><dd v-if="!module.upstream.length">无上游依赖</dd><dd v-for="(dependency, index) in module.upstream" :key="index"><strong>{{ moduleName(dependency.module_id, selected.content.modules) }}</strong>：{{ dependency.needs }}</dd></div>
            <div class="wide-field"><dt>要交给下游什么</dt><dd>{{ module.downstream_handoff || '尚未填写' }}</dd></div>
          </dl>
        </article>
      </section>
    </div>
    <p v-else-if="loaded && !loading" class="empty">{{ leader ? '项目尚未发布方案。创建整体方案，再添加术语、数据口径和分工模块。' : '项目负责人尚未发布方案。发布后可在这里查看整体方案与分工。' }}</p>

    <section v-if="loaded && (editing || selected)" class="dependency-section" aria-label="模块依赖关系">
      <h3>模块依赖关系 <span v-if="editing" class="tag">草稿</span></h3>
      <DependencyGraph :modules="graphModules" />
    </section>
  </section>
</template>

<style scoped>
.plan-panel h2 { margin: 0; }
.version-toolbar { align-items: end; margin: 20px 0; }
.version-toolbar .field { min-width: min(100%, 300px); }
.plan-editor { gap: 24px; }
fieldset { min-width: 0; margin: 0; padding: 20px; border: 1px solid #dce3eb; border-radius: 7px; }
legend { max-width: 100%; padding: 0 8px; color: #263c56; font-weight: 600; overflow-wrap: anywhere; }
.entry-fieldset { margin: 16px 0; padding: 16px; background: #fbfcfd; }
.entry-fieldset > .btn { margin-top: 14px; }
.dependency-editor { margin: 18px 0; background: #fff; }
.dependency-editor .field + .field { margin-top: 12px; }
.wide-field { grid-column: 1 / -1; }
.version-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; }
.change-note { margin: 0; padding: 14px 18px; border-left: 3px solid #28746c; background: #f1f7f5; white-space: pre-wrap; overflow-wrap: anywhere; }
.change-note strong { display: block; margin-bottom: 8px; font-size: 13px; color: #28746c; }
.read-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 20px 24px; margin: 20px 0 0; }
.read-grid dt { margin-bottom: 8px; color: #627184; font-size: 12px; }
.read-grid dd { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.8; font-size: 14px; }
.read-grid dd + dd { margin-top: 10px; }
.plan-reading h3, .dependency-section h3 { margin: 0 0 16px; font-size: 16px; }
.plan-reading h4 { margin: 0; font-size: 16px; }
.plan-reading th, .plan-reading td { white-space: pre-wrap; overflow-wrap: anywhere; vertical-align: top; }
.dependency-section { margin-top: 32px; padding-top: 24px; border-top: 1px solid #dce3eb; }
@media (max-width: 640px) { fieldset { padding: 14px; } .entry-fieldset { padding: 12px; } .read-grid { grid-template-columns: 1fr; } }
</style>
