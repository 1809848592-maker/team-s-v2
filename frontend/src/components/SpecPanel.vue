<script setup lang="ts">
import { computed, onBeforeUnmount, ref, useId, watch } from 'vue'
import { ApiError, api, dateText, errorText, projectPath, sizeText } from '../api/client'
import type { PresetCatalog, ProjectModelDisclosure, Resource, SpecDraftResult, SpecItem, SpecVersion } from '../api/types'
import ModelDisclosureNotice from './ModelDisclosureNotice.vue'

const props = defineProps<{ projectId: string; accountId: string; leader: boolean }>()
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
const generating = ref(false)
const description = ref('')
const disclosure = ref<ProjectModelDisclosure | null>(null)
const disclosureLoading = ref(false)
const disclosureError = ref('')
const consentChecked = ref(false)
const consenting = ref(false)
const changeNote = ref('')
const uploadFile = ref<File | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)
type DraftItem = SpecItem & { editorId: string }
const draft = ref<DraftItem[]>([])
let itemSequence = 0
let loadSequence = 0
let disclosureSequence = 0
let mounted = true

const busy = computed(() => publishing.value || uploading.value || generating.value || consenting.value)
function context() { return { projectId: props.projectId, accountId: props.accountId, sequence: loadSequence } }
function currentContext(owner: ReturnType<typeof context>) {
  return mounted && owner.projectId === props.projectId && owner.accountId === props.accountId && owner.sequence === loadSequence
}

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
  ++loadSequence
  const owner = context()
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
  description.value = ''
  generating.value = false
  publishing.value = false
  uploading.value = false
  consenting.value = false
  disclosure.value = null
  consentChecked.value = false
  uploadFile.value = null
  if (fileInput.value) fileInput.value.value = ''
  const path = projectPath(props.projectId)
  void loadDisclosure()
  try {
    const [published, presets, files] = await Promise.all([
      api<SpecVersion[]>(`${path}/spec/versions`),
      api<PresetCatalog>(`${path}/spec/presets`),
      api<Resource[]>(`${path}/resources`),
    ])
    if (!currentContext(owner)) return
    versions.value = published.sort((a, b) => b.version - a.version)
    catalog.value = presets
    resources.value = files
    selectedVersionId.value = current.value?.id ?? ''
    draft.value = (current.value?.items ?? []).map(editable)
  } catch (cause) {
    if (currentContext(owner)) loadError.value = errorText(cause)
  } finally {
    if (currentContext(owner)) loading.value = false
  }
}

watch(() => [props.projectId, props.accountId, props.leader], load, { immediate: true })
onBeforeUnmount(() => { mounted = false; ++loadSequence; ++disclosureSequence })

async function loadDisclosure() {
  const owner = context(), sequence = ++disclosureSequence
  disclosureLoading.value = true
  disclosureError.value = ''
  disclosure.value = null
  consentChecked.value = false
  try {
    const result = await api<ProjectModelDisclosure>(`${projectPath(owner.projectId)}/model-disclosure`)
    if (currentContext(owner) && sequence === disclosureSequence) disclosure.value = result
  } catch (cause) {
    if (currentContext(owner) && sequence === disclosureSequence) disclosureError.value = errorText(cause)
  } finally {
    if (currentContext(owner) && sequence === disclosureSequence) disclosureLoading.value = false
  }
}

async function acceptModelConsent() {
  if (!props.leader || busy.value || loading.value || !consentChecked.value || !disclosure.value?.can_consent) return
  const owner = context(), version = disclosure.value.notice.version
  consenting.value = true
  error.value = ''; notice.value = ''
  try {
    const result = await api<ProjectModelDisclosure>(`${projectPath(owner.projectId)}/model-consent`, { disclosure_version: version })
    if (!currentContext(owner)) return
    disclosure.value = result
    consentChecked.value = false
    notice.value = '负责人知情同意已记录，可以辅助起草规范。'
    emit('changed')
  } catch (cause) {
    if (!currentContext(owner)) return
    error.value = errorText(cause)
    if (cause instanceof ApiError && cause.status === 409) await loadDisclosure()
  } finally {
    if (currentContext(owner)) consenting.value = false
  }
}

async function generateDraft() {
  if (!props.leader || busy.value || loading.value || disclosureLoading.value || !disclosure.value?.project_consent || !description.value.trim() || draft.value.length >= 500) return
  const owner = context(), version = disclosure.value.notice.version
  generating.value = true
  error.value = ''; notice.value = ''
  try {
    const result = await api<SpecDraftResult>(`${projectPath(owner.projectId)}/spec/draft`, {
      description: description.value.trim(), request_id: crypto.randomUUID(), disclosure_version: version,
    })
    if (!currentContext(owner)) return
    if (!result.items.length || draft.value.length + result.items.length > 500) {
      error.value = '生成的条目无法完整加入：一版规范最多 500 条。原草稿已保留，请减少草稿条目后重新生成。'
      return
    }
    const used = new Set(draft.value.map(item => item.number.trim()))
    let renamed = 0
    const additions = result.items.map(item => {
      const number = uniqueNumber(item.number, used)
      if (number !== item.number) ++renamed
      used.add(number)
      return editable({ ...item, number })
    })
    draft.value.push(...additions)
    notice.value = `已将 ${additions.length} 条模型建议加入草稿，尚未发布。${renamed ? `其中 ${renamed} 条编号与原草稿重复，已添加后缀。` : ''}请逐条核对、修改后手动发布。`
    emit('changed')
  } catch (cause) {
    if (!currentContext(owner)) return
    error.value = `${errorText(cause)} 原草稿和要求描述已保留。`
    if (cause instanceof ApiError && cause.status === 409) await loadDisclosure()
  } finally {
    if (currentContext(owner)) generating.value = false
  }
}

function uniqueNumber(proposed: string, used: Set<string>): string {
  if (!used.has(proposed)) return proposed
  let suffix = 2
  while (used.has(`${proposed.slice(0, 115)}-${suffix}`)) suffix++
  return `${proposed.slice(0, 115)}-${suffix}`
}

function addPresets() {
  if (!props.leader || busy.value) return
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
  if (!props.leader || busy.value || draft.value.length >= 500) return
  const number = uniqueNumber('SPEC-01', new Set(draft.value.map(item => item.number.trim())))
  draft.value.push(editable({
    number, category: 'writing', requirement: '', check_method: 'manual',
    positive_example: '', negative_example: '', template_resource_id: null,
  }))
}

function removeItem(index: number) {
  if (props.leader && !busy.value) draft.value.splice(index, 1)
}

async function publish() {
  if (!props.leader || busy.value) return
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
  const owner = context(), projectId = owner.projectId
  try {
    const published = await api<SpecVersion>(`${projectPath(projectId)}/spec/versions`, {
      change_note: changeNote.value.trim(), items,
    })
    if (!currentContext(owner)) return
    versions.value = [published, ...versions.value.filter(item => item.id !== published.id)]
      .sort((a, b) => b.version - a.version)
    selectedVersionId.value = published.id
    draft.value = published.items.map(editable)
    changeNote.value = ''
    notice.value = `规范 v${published.version} 已发布并冻结。后续修改将发布为新版本。`
    emit('changed')
  } catch (cause) {
    if (currentContext(owner)) error.value = errorText(cause)
  } finally {
    if (currentContext(owner)) publishing.value = false
  }
}

function selectFile(event: Event) {
  uploadFile.value = (event.target as HTMLInputElement).files?.[0] ?? null
}

async function upload() {
  if (!props.leader || !uploadFile.value || busy.value) return
  error.value = ''
  notice.value = ''
  uploading.value = true
  const owner = context(), projectId = owner.projectId
  const form = new FormData()
  form.append('file', uploadFile.value)
  try {
    const resource = await api<Resource>(`${projectPath(projectId)}/resources`, form)
    if (!currentContext(owner)) return
    resources.value = [resource, ...resources.value.filter(item => item.id !== resource.id)]
    uploadFile.value = null
    if (fileInput.value) fileInput.value.value = ''
    notice.value = `模板“${resource.filename}”已上传，可在规范条目中选择。`
    emit('changed')
    try {
      const refreshed = await api<Resource[]>(`${projectPath(projectId)}/resources`)
      if (currentContext(owner)) resources.value = refreshed
    } catch (cause) {
      if (currentContext(owner)) error.value = `模板已上传，列表刷新失败：${errorText(cause)}`
    }
  } catch (cause) {
    if (currentContext(owner)) error.value = errorText(cause)
  } finally {
    if (currentContext(owner)) uploading.value = false
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

        <section class="spec-assistant" :aria-labelledby="`${prefix}-assistant-title`" data-spec-assistant>
          <h3 :id="`${prefix}-assistant-title`">平台辅助起草</h3>
          <p class="muted">描述你的要求，生成条目后逐条核对、修改，再手动发布。每次生成调用一次模型，计入项目额度和用量；本次只发送下方要求描述。</p>
          <p v-if="disclosureLoading" class="muted" role="status">正在读取数据外发告知…</p>
          <p v-if="disclosureError" class="error" role="alert">{{ disclosureError }} <button class="btn quiet" type="button" :disabled="busy || disclosureLoading" @click="loadDisclosure">重试告知</button></p>
          <template v-if="disclosure">
            <ModelDisclosureNotice :notice="disclosure.notice" compact/>
            <p v-if="disclosure.project_consent" class="muted">项目负责人已于 {{ dateText(disclosure.project_consent.accepted_at) }} 确认本版数据外发告知。</p>
            <template v-else>
              <p class="muted">本项目尚未同意当前数据外发告知，辅助起草暂未启用。人工编辑和发布规范仍可使用。</p>
              <form v-if="disclosure.can_consent" class="assistant-consent" @submit.prevent="acceptModelConsent">
                <label class="consent-checkbox"><input v-model="consentChecked" type="checkbox" :disabled="busy">我已阅读并同意向 DeepSeek 外发审核及规范起草数据。</label>
                <button class="btn" :disabled="busy || !consentChecked">{{ consenting ? '正在记录…' : '确认知情同意' }}</button>
              </form>
            </template>
          </template>
          <form class="stack assistant-form" @submit.prevent="generateDraft">
            <div class="field">
              <label :for="`${prefix}-draft-description`">描述规范要求</label>
              <textarea :id="`${prefix}-draft-description`" v-model="description" rows="4" required maxlength="6000" :disabled="busy" placeholder="例如：用客观、简洁的中文写研究汇报；缩写首次出现写全称；交接时附数据来源和未解决事项。" data-spec-description/>
            </div>
            <div class="toolbar"><button class="btn" :disabled="busy || disclosureLoading || !disclosure?.project_consent || !description.trim() || draft.length >= 500" data-spec-generate>{{ generating ? '正在起草…' : '生成条目草稿' }}</button><span class="muted">生成结果加入现有草稿，不自动发布；失败后可手动重试。</span></div>
          </form>
        </section>

        <fieldset class="preset-fieldset" :disabled="busy">
          <legend>选择范例</legend>
          <div class="preset-grid">
            <label v-for="preset in catalog.presets" :key="preset.key" :for="`${prefix}-preset-${preset.key}`" class="preset-option">
              <input :id="`${prefix}-preset-${preset.key}`" v-model="selectedPresets" type="checkbox" :value="preset.key">
              <span><strong>{{ preset.name }}</strong><small>{{ preset.items.length }} 条 · 可修改</small></span>
            </label>
          </div>
          <button class="btn quiet" type="button" :disabled="!selectedPresets.length || busy" @click="addPresets">将所选范例加入草稿</button>
        </fieldset>

        <form class="stack draft-form" @submit.prevent="publish">
          <p v-if="!draft.length" class="empty">草稿中还没有条目。可加入范例，也可添加自己的规范。</p>
          <fieldset v-for="(item, index) in draft" :key="item.editorId" class="item-editor" :disabled="busy">
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
            <button class="btn quiet" type="button" :disabled="busy || draft.length >= 500" @click="addItem">添加条目</button>
            <span class="muted">{{ draft.length }} / 500 条</span>
          </div>
          <div class="field">
            <label :for="`${prefix}-change-note`">本次变更说明</label>
            <textarea :id="`${prefix}-change-note`" v-model="changeNote" rows="2" required maxlength="2000" :disabled="busy" placeholder="说明本版新增或修改了哪些要求" />
          </div>
          <div class="toolbar">
            <button class="btn primary" type="submit" :disabled="busy || !draft.length">{{ publishing ? '正在发布…' : '发布规范新版本' }}</button>
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
          <div class="field"><label :for="`${prefix}-file`">上传自有模板</label><input :id="`${prefix}-file`" ref="fileInput" type="file" required :disabled="busy" @change="selectFile"></div>
          <button class="btn" type="submit" :disabled="!uploadFile || busy">{{ uploading ? '正在上传…' : '上传模板' }}</button>
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
.spec-assistant { display: grid; gap: 1rem; margin-top: 1.5rem; padding: 1.2rem; border: 1px solid #dce3eb; border-left: 3px solid #28746c; border-radius: 6px; }
.spec-assistant p { margin: 0; line-height: 1.75; }
.assistant-consent { display: flex; align-items: center; gap: 1rem; flex-wrap: wrap; }
.consent-checkbox { display: flex; align-items: flex-start; gap: .5rem; font-size: .85rem; line-height: 1.75; }
.consent-checkbox input { flex-shrink: 0; margin-top: .35rem; }
.assistant-form .toolbar { justify-content: flex-start; }
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
