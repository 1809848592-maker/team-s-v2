<script setup lang="ts">
import { computed, ref, useId, watch } from 'vue'
import { api, dateText, errorText, projectPath, sizeText } from '../api/client'
import type { PresetCatalog, Resource, SpecItem, SpecVersion } from '../api/types'

const props = defineProps<{ projectId: string; leader: boolean }>()
const emit = defineEmits<{ changed: [] }>()
const prefix = useId()
const versions = ref<SpecVersion[]>([])
const catalog = ref<PresetCatalog>({ presets: [], templates: [] })
const resources = ref<Resource[]>([])
const selectedVersionId = ref('')
const selectedPresets = ref<string[]>([])
const loading = ref(true)
const loadError = ref('')
const error = ref('')
const notice = ref('')
const publishing = ref(false)
const uploading = ref(false)
const changeNote = ref('')
const uploadFile = ref<File | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)
type DraftItem = SpecItem & { editorId: string }
const draft = ref<DraftItem[]>([])
let itemSequence = 0
let loadSequence = 0

const categories: Record<SpecItem['category'], string> = {
  writing: '文字规范', template: '模板使用', format: '格式要求', handoff: '交接规范',
}
const methods: Record<SpecItem['check_method'], string> = {
  program: '程序检查', ai: 'AI 检查', manual: '仅人工',
}
const current = computed(() => versions.value[0] ?? null)
const selectedVersion = computed(() => versions.value.find(item => item.id === selectedVersionId.value) ?? null)
const templateResources = computed(() => {
  const byId = new Map<string, Resource>()
  for (const resource of [...catalog.value.templates, ...resources.value]) byId.set(resource.id, resource)
  return [...byId.values()]
})
const builtinTemplates = computed(() => templateResources.value.filter(item => item.builtin))
const projectTemplates = computed(() => templateResources.value.filter(item => !item.builtin))
const resourceById = computed(() => new Map(templateResources.value.map(item => [item.id, item])))

function editable(item: SpecItem): DraftItem {
  return { ...item, editorId: `${prefix}-item-${++itemSequence}` }
}

async function load() {
  const sequence = ++loadSequence
  loading.value = true
  loadError.value = ''
  error.value = ''
  notice.value = ''
  versions.value = []
  catalog.value = { presets: [], templates: [] }
  resources.value = []
  draft.value = []
  selectedPresets.value = []
  selectedVersionId.value = ''
  changeNote.value = ''
  uploadFile.value = null
  if (fileInput.value) fileInput.value.value = ''
  const path = projectPath(props.projectId)
  try {
    const [published, presets, files] = await Promise.all([
      api<SpecVersion[]>(`${path}/spec/versions`),
      api<PresetCatalog>(`${path}/spec/presets`),
      api<Resource[]>(`${path}/resources`),
    ])
    if (sequence !== loadSequence) return
    versions.value = published.sort((a, b) => b.version - a.version)
    catalog.value = presets
    resources.value = files
    selectedVersionId.value = current.value?.id ?? ''
    draft.value = (current.value?.items ?? []).map(editable)
  } catch (cause) {
    if (sequence === loadSequence) loadError.value = errorText(cause)
  } finally {
    if (sequence === loadSequence) loading.value = false
  }
}

watch(() => props.projectId, load, { immediate: true })

function uniqueNumber(proposed: string, used: Set<string>): string {
  if (!used.has(proposed)) return proposed
  let suffix = 2
  while (used.has(`${proposed.slice(0, 115)}-${suffix}`)) suffix++
  return `${proposed.slice(0, 115)}-${suffix}`
}

function addPresets() {
  if (!props.leader || publishing.value) return
  const additions = catalog.value.presets
    .filter(item => selectedPresets.value.includes(item.key))
    .flatMap(item => item.items)
  if (!additions.length) return
  error.value = ''
  if (draft.value.length + additions.length > 500) {
    error.value = '一版规范最多包含 500 条，请减少所选范例或删除部分草稿条目。'
    return
  }
  const used = new Set(draft.value.map(item => item.number.trim()))
  for (const item of additions) {
    const number = uniqueNumber(item.number, used)
    used.add(number)
    draft.value.push(editable({ ...item, number }))
  }
  selectedPresets.value = []
  notice.value = `已将 ${additions.length} 条范例加入草稿，可继续修改后发布。`
}

function addItem() {
  if (!props.leader || publishing.value || draft.value.length >= 500) return
  const number = uniqueNumber('SPEC-01', new Set(draft.value.map(item => item.number.trim())))
  draft.value.push(editable({
    number, category: 'writing', requirement: '', check_method: 'manual',
    positive_example: '', negative_example: '', template_resource_id: null,
  }))
}

function removeItem(index: number) {
  if (props.leader && !publishing.value) draft.value.splice(index, 1)
}

async function publish() {
  if (!props.leader || publishing.value) return
  error.value = ''
  notice.value = ''
  const items: SpecItem[] = draft.value.map(({ editorId: _editorId, ...item }) => ({
    ...item, number: item.number.trim(), requirement: item.requirement.trim(),
  }))
  if (!items.length) {
    error.value = '请先加入范例或添加至少一条规范。'
    return
  }
  if (!changeNote.value.trim() || items.some(item => !item.number || !item.requirement)) {
    error.value = '请填写每条规范的编号、要求，以及本次变更说明。'
    return
  }
  if (new Set(items.map(item => item.number)).size !== items.length) {
    error.value = '规范编号不能重复，请修改重复编号后发布。'
    return
  }
  publishing.value = true
  const projectId = props.projectId
  try {
    const published = await api<SpecVersion>(`${projectPath(projectId)}/spec/versions`, {
      change_note: changeNote.value.trim(), items,
    })
    if (projectId !== props.projectId) return
    versions.value = [published, ...versions.value.filter(item => item.id !== published.id)]
      .sort((a, b) => b.version - a.version)
    selectedVersionId.value = published.id
    draft.value = published.items.map(editable)
    changeNote.value = ''
    notice.value = `规范 v${published.version} 已发布并冻结。后续修改将发布为新版本。`
    emit('changed')
  } catch (cause) {
    if (projectId === props.projectId) error.value = errorText(cause)
  } finally {
    publishing.value = false
  }
}

function selectFile(event: Event) {
  uploadFile.value = (event.target as HTMLInputElement).files?.[0] ?? null
}

async function upload() {
  if (!props.leader || !uploadFile.value || uploading.value) return
  error.value = ''
  notice.value = ''
  uploading.value = true
  const projectId = props.projectId
  const form = new FormData()
  form.append('file', uploadFile.value)
  try {
    const resource = await api<Resource>(`${projectPath(projectId)}/resources`, form)
    if (projectId !== props.projectId) return
    resources.value = [resource, ...resources.value.filter(item => item.id !== resource.id)]
    uploadFile.value = null
    if (fileInput.value) fileInput.value.value = ''
    notice.value = `模板“${resource.filename}”已上传，可在规范条目中选择。`
    emit('changed')
    try {
      const refreshed = await api<Resource[]>(`${projectPath(projectId)}/resources`)
      if (projectId === props.projectId) resources.value = refreshed
    } catch (cause) {
      if (projectId === props.projectId) error.value = `模板已上传，列表刷新失败：${errorText(cause)}`
    }
  } catch (cause) {
    if (projectId === props.projectId) error.value = errorText(cause)
  } finally {
    uploading.value = false
  }
}
</script>

<template>
  <div class="stack spec-panel">
    <p v-if="loading" class="muted" role="status">正在读取规范与模板…</p>
    <div v-else-if="loadError" class="panel">
      <p class="error" role="alert">{{ loadError }}</p>
      <button class="btn" type="button" @click="load">重新读取规范</button>
    </div>
    <template v-else>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <p v-if="notice" class="notice" role="status">{{ notice }}</p>

      <section class="panel" :aria-labelledby="`${prefix}-published-title`">
        <div class="panel-heading">
          <div>
            <h2 :id="`${prefix}-published-title`">已发布规范</h2>
            <p class="muted">发布即冻结，团队按同一版本交付与交接。</p>
          </div>
          <span v-if="current" class="tag">当前 v{{ current.version }}</span>
        </div>
        <p v-if="!current" class="empty">尚未发布规范。{{ leader ? '从下方范例起草首版规范。' : '等待项目负责人发布后，可在此查看。' }}</p>
        <template v-else>
          <div class="field version-picker">
            <label :for="`${prefix}-version`">查看规范版本</label>
            <select :id="`${prefix}-version`" v-model="selectedVersionId">
              <option v-for="version in versions" :key="version.id" :value="version.id">
                v{{ version.version }}{{ version.id === current.id ? ' · 当前版本' : ' · 历史版本' }} · {{ dateText(version.published_at) }}
              </option>
            </select>
          </div>
          <template v-if="selectedVersion">
            <div class="version-summary">
              <strong>v{{ selectedVersion.version }} · {{ selectedVersion.id === current.id ? '当前规范' : '历史快照' }}</strong>
              <span class="muted">{{ dateText(selectedVersion.published_at) }} · {{ selectedVersion.items.length }} 条规范 · 只读</span>
              <p class="preserve">{{ selectedVersion.change_note }}</p>
            </div>
            <div class="stack">
              <article v-for="item in selectedVersion.items" :key="item.number" class="record spec-record">
                <div class="toolbar">
                  <strong class="item-number">{{ item.number }}</strong>
                  <span class="tag">{{ categories[item.category] }}</span>
                  <span class="muted">{{ methods[item.check_method] }}</span>
                </div>
                <p class="preserve">{{ item.requirement }}</p>
                <dl v-if="item.positive_example || item.negative_example" class="examples">
                  <div v-if="item.positive_example"><dt>正例</dt><dd>{{ item.positive_example }}</dd></div>
                  <div v-if="item.negative_example"><dt>反例</dt><dd>{{ item.negative_example }}</dd></div>
                </dl>
                <template v-if="item.template_resource_id">
                  <a v-if="resourceById.get(item.template_resource_id)" class="template-link" :href="resourceById.get(item.template_resource_id)!.download_url" download>
                    下载模板：{{ resourceById.get(item.template_resource_id)!.filename }}
                  </a>
                  <p v-else class="muted">关联模板暂不可用。</p>
                </template>
              </article>
            </div>
          </template>
        </template>
      </section>

      <section v-if="leader" class="panel" :aria-labelledby="`${prefix}-draft-title`">
        <div class="panel-heading">
          <div>
            <h2 :id="`${prefix}-draft-title`">{{ current ? '起草下一版规范' : '起草首版规范' }}</h2>
            <p class="muted">{{ current ? `草稿基于当前 v${current.version}，查看历史不会改变草稿。` : '勾选范例后加入条目，再按项目需要修改。' }}</p>
          </div>
          <span class="tag">未发布草稿</span>
        </div>

        <fieldset class="preset-fieldset" :disabled="publishing">
          <legend>选择范例</legend>
          <div class="preset-grid">
            <label v-for="preset in catalog.presets" :key="preset.key" :for="`${prefix}-preset-${preset.key}`" class="preset-option">
              <input :id="`${prefix}-preset-${preset.key}`" v-model="selectedPresets" type="checkbox" :value="preset.key">
              <span><strong>{{ preset.name }}</strong><small>{{ preset.items.length }} 条 · 可修改</small></span>
            </label>
          </div>
          <button class="btn quiet" type="button" :disabled="!selectedPresets.length || publishing" @click="addPresets">将所选范例加入草稿</button>
        </fieldset>

        <form class="stack draft-form" @submit.prevent="publish">
          <p v-if="!draft.length" class="empty">草稿中还没有条目。可加入范例，也可添加自己的规范。</p>
          <fieldset v-for="(item, index) in draft" :key="item.editorId" class="item-editor" :disabled="publishing">
            <legend>条目 {{ index + 1 }}</legend>
            <div class="form-grid">
              <div class="field">
                <label :for="`${item.editorId}-number`">编号</label>
                <input :id="`${item.editorId}-number`" v-model="item.number" required maxlength="120" autocomplete="off">
              </div>
              <div class="field">
                <label :for="`${item.editorId}-category`">类别</label>
                <select :id="`${item.editorId}-category`" v-model="item.category">
                  <option v-for="(label, value) in categories" :key="value" :value="value">{{ label }}</option>
                </select>
              </div>
              <div class="field">
                <label :for="`${item.editorId}-method`">检查方式</label>
                <select :id="`${item.editorId}-method`" v-model="item.check_method">
                  <option v-for="(label, value) in methods" :key="value" :value="value">{{ label }}</option>
                </select>
              </div>
            </div>
            <div class="field">
              <label :for="`${item.editorId}-requirement`">要求</label>
              <textarea :id="`${item.editorId}-requirement`" v-model="item.requirement" rows="3" required maxlength="10000" />
            </div>
            <div class="form-grid">
              <div class="field">
                <label :for="`${item.editorId}-positive`">正例（可选）</label>
                <textarea :id="`${item.editorId}-positive`" v-model="item.positive_example" rows="3" maxlength="10000" />
              </div>
              <div class="field">
                <label :for="`${item.editorId}-negative`">反例（可选）</label>
                <textarea :id="`${item.editorId}-negative`" v-model="item.negative_example" rows="3" maxlength="10000" />
              </div>
            </div>
            <div class="field">
              <label :for="`${item.editorId}-template`">关联模板（可选）</label>
              <select :id="`${item.editorId}-template`" v-model="item.template_resource_id">
                <option :value="null">不关联模板</option>
                <option v-for="resource in templateResources" :key="resource.id" :value="resource.id">{{ resource.filename }}{{ resource.builtin ? ' · 内置' : '' }}</option>
              </select>
            </div>
            <div class="toolbar item-actions">
              <a v-if="item.template_resource_id && resourceById.get(item.template_resource_id)" :href="resourceById.get(item.template_resource_id)!.download_url" download>下载关联模板</a>
              <button class="btn danger" type="button" :aria-label="`删除条目 ${index + 1}：${item.number}`" @click="removeItem(index)">删除条目</button>
            </div>
          </fieldset>
          <div class="toolbar">
            <button class="btn quiet" type="button" :disabled="publishing || draft.length >= 500" @click="addItem">添加条目</button>
            <span class="muted">{{ draft.length }} / 500 条</span>
          </div>
          <div class="field">
            <label :for="`${prefix}-change-note`">本次变更说明</label>
            <textarea :id="`${prefix}-change-note`" v-model="changeNote" rows="2" required maxlength="2000" :disabled="publishing" placeholder="说明本版新增或修改了哪些要求" />
          </div>
          <div class="toolbar">
            <button class="btn primary" type="submit" :disabled="publishing || !draft.length">{{ publishing ? '正在发布…' : '发布规范新版本' }}</button>
            <span class="muted">发布后保留冻结快照，后续修改需发布新版本。</span>
          </div>
        </form>
      </section>

      <section class="panel" :aria-labelledby="`${prefix}-templates-title`">
        <div class="panel-heading">
          <div><h2 :id="`${prefix}-templates-title`">模板文件</h2><p class="muted">下载内置模板，或使用项目自行上传的模板。</p></div>
        </div>
        <div class="template-groups">
          <div>
            <h3>内置模板</h3>
            <ul class="template-list">
              <li v-for="resource in builtinTemplates" :key="resource.id"><a :href="resource.download_url" download>{{ resource.filename }}</a><span class="muted">{{ sizeText(resource.size) }}</span></li>
            </ul>
            <p v-if="!builtinTemplates.length" class="empty">暂无内置模板。</p>
          </div>
          <div>
            <h3>项目模板</h3>
            <ul v-if="projectTemplates.length" class="template-list">
              <li v-for="resource in projectTemplates" :key="resource.id"><a :href="resource.download_url" download>{{ resource.filename }}</a><span class="muted">{{ sizeText(resource.size) }}</span></li>
            </ul>
            <p v-else class="empty">尚未上传项目模板。</p>
          </div>
        </div>
        <form v-if="leader" class="upload-form" @submit.prevent="upload">
          <div class="field"><label :for="`${prefix}-file`">上传自有模板</label><input :id="`${prefix}-file`" ref="fileInput" type="file" required :disabled="uploading" @change="selectFile"></div>
          <button class="btn" type="submit" :disabled="!uploadFile || uploading">{{ uploading ? '正在上传…' : '上传模板' }}</button>
        </form>
      </section>
    </template>
  </div>
</template>

<style scoped>
.spec-panel h2, .spec-panel h3 { margin: 0; }
.spec-panel h3 { font-size: 1rem; }
.panel-heading p { margin: .5rem 0 0; }
.version-picker { max-width: 34rem; margin: 1.3rem 0; }
.version-summary { display: grid; gap: .5rem; padding: 1rem 0 1.3rem; border-top: 1px solid #dce3eb; }
.version-summary p { margin: 0; }
.preserve { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.75; }
.item-number { overflow-wrap: anywhere; }
.spec-record { border-left: 3px solid #28746c; }
.examples { display: grid; gap: .8rem; margin: 1rem 0; }
.examples > div { display: grid; grid-template-columns: 2.5rem minmax(0, 1fr); gap: .65rem; }
.examples dt { color: #627184; font-size: .85rem; }
.examples dd { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.7; }
.template-link { display: inline-block; overflow-wrap: anywhere; }
.preset-fieldset, .item-editor { min-width: 0; margin: 1.5rem 0 0; padding: 1rem 1.2rem 1.2rem; border: 1px solid #dce3eb; border-radius: 6px; }
.preset-fieldset legend, .item-editor legend { padding: 0 .45rem; font-weight: 600; }
.preset-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .6rem; margin-bottom: 1rem; }
.preset-option { display: flex; align-items: flex-start; gap: .65rem; padding: .7rem; background: #f5f7fa; border-radius: 4px; cursor: pointer; }
.preset-option input { width: 1rem; height: 1rem; margin: .2rem 0 0; flex: 0 0 auto; accent-color: #28746c; }
.preset-option strong { font-size: .9rem; font-weight: 500; }
.preset-option small { display: block; margin-top: .3rem; color: #627184; }
.draft-form { margin-top: 1rem; }
.item-editor { display: grid; gap: 1rem; margin: 0; }
.item-actions { justify-content: flex-end; }
.item-actions a { margin-right: auto; }
.template-groups { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 2rem; margin-top: 1.5rem; }
.template-list { list-style: none; margin: .75rem 0 0; padding: 0; }
.template-list li { display: flex; flex-wrap: wrap; justify-content: space-between; gap: .4rem 1rem; padding: .8rem 0; border-bottom: 1px solid #dce3eb; }
.template-list a { overflow-wrap: anywhere; }
.upload-form { display: flex; flex-wrap: wrap; gap: 1rem; align-items: end; margin-top: 1.5rem; padding-top: 1.5rem; border-top: 1px solid #dce3eb; }
.upload-form .field { flex: 1 1 15rem; }
@media (max-width: 640px) {
  .preset-grid, .template-groups { grid-template-columns: 1fr; }
  .preset-fieldset, .item-editor { padding: .85rem; }
  .template-groups { gap: 1.3rem; }
}
</style>
