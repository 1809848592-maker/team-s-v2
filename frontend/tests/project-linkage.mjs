import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { readFile, mkdtemp, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { createSSRApp, createRenderer, nextTick } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createServer } from 'vite'
import { parse, compileScript } from '@vue/compiler-sfc'
import { build } from 'esbuild'

const root = new URL('../', import.meta.url).pathname
const require = createRequire(import.meta.url)
const provenance = { reviewer: 'project_linkage', instruction_version: 'test-v1', model: 'fixed-model', input_hash: 'a'.repeat(64) }
const finding = { ...provenance, id: 'f1', check_key: 'contradiction', category: 'project_linkage', severity: 'important', module_ids: ['m1'], problem: '<script>问题</script>', excerpt: '<img src=x onerror=alert(1)>', basis: '与方案口径不同', suggestion: '统一定义', evidence_status: 'verified', request_id: null }
const result = { coverage: 'completed', coverage_notes: [], modules: [{ module_id: 'm1', module_name: '模块甲', delivery_id: 'd1', delivery_version: 2, plan_version_id: 'plan-1', spec_version_id: 'spec-1', plan_outdated: false, spec_outdated: false, state: 'summarized', reason: null, summary: '模块摘要', provenance }], findings: [finding], discarded_findings: 0, comparison: { state: 'completed', provenance } }
const review = { id: 'r1', project_id: 'p1', requested_by: 'u1', plan_version_id: 'plan-2', plan_version: 2, spec_version_id: 'spec-2', spec_version: 2, status: 'completed', attempt: 1, max_attempts: 3, error_code: null, message: '审核已完成', created_at: '2026-11-12T00:00:00Z', completed_at: '2026-11-12T00:00:01Z', can_retry: false, can_convert: true, result }
let groups = 0
const server = await createServer({ root, server: { middlewareMode: true, hmr: false, ws: false }, optimizeDeps: { noDiscovery: true, include: [] }, appType: 'custom' })
try {
  const { default: Report } = await server.ssrLoadModule('/src/components/ProjectLinkageReport.vue')
  const render = (data = result, canConvert = false, disabled = false) => renderToString(createSSRApp(Report, { result: data, canConvert, disabled }))
  const complete = await render()
  assert.match(complete, /审核流程已完成/)
  assert.match(complete, /AI 生成，供参考/)
  assert.match(complete, /已采用交付 v2/)
  assert.ok(!complete.includes('转为协作需求'))
  assert.ok(!complete.includes('<script>问题</script>'))
  assert.match(complete, /&lt;script&gt;问题&lt;\/script&gt;/)
  assert.ok(!complete.includes('<img src=x'))
  groups++
  const leader = await render(result, true)
  assert.match(leader, /转为协作需求/)
  const disabled = await render(result, true, true)
  assert.match(disabled, /disabled[^>]*>转为协作需求/)
  groups++
  const partial = await render({ ...result, coverage: 'incomplete', modules: [{ ...result.modules[0], delivery_id: null, delivery_version: null, state: 'uncovered', reason: '没有已采用交付', summary: null }], findings: [], comparison: { state: 'uncovered', provenance: null }, coverage_notes: ['模块尚未覆盖'] })
  assert.match(partial, /没有已采用交付/)
  assert.match(partial, /未完成、未覆盖的范围不作通过判断/)
  assert.match(partial, /整体比对：未覆盖/)
  assert.ok(!partial.includes('审核流程已完成'))
  groups++
  const failed = await render({ ...result, coverage: 'incomplete', modules: [{ ...result.modules[0], state: 'failed', reason: '文件读取失败' }], findings: [], comparison: { state: 'incomplete', provenance: null } })
  assert.match(failed, /文件读取失败/)
  assert.match(failed, /整体比对：未完成/)
  assert.match(failed, /未完成或未覆盖范围仍需处理/)
  groups++
  const evidence = await render({ ...result, discarded_findings: 2, modules: [{ ...result.modules[0], plan_outdated: true, spec_outdated: true }], findings: [{ ...finding, evidence_status: 'unverified' }] })
  assert.match(evidence, /交付依据旧版方案 \/ 规范/)
  assert.match(evidence, /data-evidence-unverified/)
  assert.match(evidence, /2 条意见因格式无效未采用/)
  groups++
  const converted = await render({ ...result, findings: [{ ...finding, request_id: 'request-1' }] }, true)
  assert.match(converted, /已转为协作需求 · 查看需求/)
  assert.ok(!converted.includes('>转为协作需求<'))
  groups++
  const indexed = await render({ ...result,
    modules: [{ ...result.modules[0], source_segments: [
      { segment_id: 'S1', location: { part: 'word/document.xml', paragraph: 2 } },
      { segment_id: 'S2', location: { part: 'pdf', page: 4 } },
      { segment_id: 'S3', location: { part: 'ppt/slides/slide3.xml', slide: 3, shape_id: '7', paragraph: 1 } },
      { segment_id: 'S4', location: { part: 'handoff_note' } },
      { segment_id: 'S5', location: { part: 'markdown', line_start: 8, line_end: 10 } },
    ] }],
    comparison: { ...result.comparison, checks: [
      { key: 'connection', status: 'checked', reason: '已对摘要核对' },
      { key: 'overlap', status: 'uncovered', reason: '<b>未覆盖</b>' },
      { key: 'contradiction', status: 'issues', reason: '有意见' },
    ] },
  })
  assert.match(indexed, /S1[^<]*正文 · 第2段/)
  assert.match(indexed, /S2[^<]*文档 · 第4页/)
  assert.match(indexed, /S3[^<]*幻灯片 · 第3张 · 第1段 · 图形7/)
  assert.match(indexed, /S4[^<]*交接说明/)
  assert.match(indexed, /S5[^<]*文稿 · 第8至10行/)
  assert.ok(!indexed.includes('word/document.xml'))
  assert.match(indexed, /上下游衔接/)
  assert.match(indexed, /已核对摘要/)
  assert.match(indexed, /有审核意见/)
  assert.match(indexed, /&lt;b&gt;未覆盖&lt;\/b&gt;/)
  assert.match(indexed, /摘要摘录已核对，原文仍需复核/)
  groups++
  const empty = await render({ ...result, coverage: 'incomplete', modules: [], findings: [] })
  assert.match(empty, /本次没有可审核的模块，未覆盖/)
  assert.ok(!empty.includes('已发现内容完全正确'))
  groups++
} finally { await server.close() }

// Compile the actual client component and mount it with Vue's in-memory renderer.
// Fixed fetch responses exercise actions and cancellation without a browser or network.
const directory = await mkdtemp(join(tmpdir(), 'teams-linkage-ui-'))
const originalFetch = globalThis.fetch, originalDocument = globalThis.document, originalDocumentClass = globalThis.Document, originalShadowRoot = globalThis.ShadowRoot
let listeners = new Map(), calls = [], deferredResolve, cancelledSignal
const documentStub = { visibilityState: 'visible', addEventListener: (type, handler) => listeners.set(type, handler), removeEventListener: (type, handler) => { if (listeners.get(type) === handler) listeners.delete(type) } }
globalThis.document = documentStub
globalThis.Document = class {}
globalThis.ShadowRoot = class {}
const clone = value => JSON.parse(JSON.stringify(value))
let saved = clone(review)
globalThis.fetch = async (path, options) => {
  calls.push({ path, options })
  if (String(path).includes('/p2/')) {
    cancelledSignal = options.signal
    return new Promise(resolve => { deferredResolve = resolve })
  }
  if (String(path).endsWith('/retry')) {
    saved = { ...saved, status: 'queued', can_retry: false, can_convert: false, attempt: 0, message: '等待后台处理' }
    return { ok: true, status: 200, json: async () => clone(saved) }
  }
  if (String(path).endsWith('/findings/f1/request')) {
    saved.result.findings[0].request_id = 'new-request'
    return { ok: true, status: 200, json: async () => ({ id: 'new-request' }) }
  }
  const data = String(path).endsWith('/linkage-reviews') && options.method === 'GET' ? [saved] : saved
  return { ok: true, status: 200, json: async () => clone(data) }
}
let app
try {
  const compiled = await build({ entryPoints: [join(root, 'src/components/ProjectLinkagePanel.vue')], bundle: true, platform: 'node', format: 'esm', write: false, plugins: [{ name: 'vue-inline', setup(builder) {
    builder.onResolve({ filter: /^vue$/ }, () => ({ path: require.resolve('vue'), external: true }))
    builder.onLoad({ filter: /\.vue$/ }, async ({ path }) => {
      const { descriptor } = parse(await readFile(path, 'utf8'), { filename: path })
      const code = compileScript(descriptor, { id: path, inlineTemplate: true }).content
      return { contents: code, loader: 'ts', resolveDir: join(path, '..') }
    })
  } }] })
  const bundlePath = join(directory, 'panel.mjs')
  await writeFile(bundlePath, compiled.outputFiles[0].text)
  const { default: Panel } = await import(pathToFileURL(bundlePath).href)
  const makeNode = (type, text = '') => ({
    type, text, children: [], props: {}, parent: null, listeners: new Map(), selectedIndex: -1,
    addEventListener(name, handler) { this.listeners.set(name, handler) },
    removeEventListener(name) { this.listeners.delete(name) },
    getRootNode() { return {} },
    get options() { return this.children.flatMap(child => child.type === 'option' ? [child] : child.options) },
  })
  const renderer = createRenderer({
    createElement: type => makeNode(type), createText: text => makeNode('#text', text), createComment: text => makeNode('#comment', text),
    setText: (node, text) => { node.text = text }, setElementText: (node, text) => { node.text = text; node.children = [] },
    patchProp: (node, key, _old, value) => { node.props[key] = value; if (key === 'value' || key === 'multiple') node[key] = value }, parentNode: node => node.parent,
    nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
    insert: (node, parent, anchor) => { if (node.parent) { const previous = node.parent.children.indexOf(node); if (previous >= 0) node.parent.children.splice(previous, 1) } const index = anchor ? parent.children.indexOf(anchor) : -1; parent.children.splice(index < 0 ? parent.children.length : index, 0, node); node.parent = parent },
    remove: node => { if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1); node.parent = null },
  })
  const container = makeNode('root')
  const props = { projectId: 'p1', accountId: 'u1', leader: true, members: [{ user_id: 'u1', username: '甲', role: 'leader' }], modules: [{ id: 'm1', name: '模块甲' }] }
  app = renderer.createApp(Panel, props); app.mount(container)
  const settle = async () => { for (let n = 0; n < 8; n++) { await new Promise(resolve => setImmediate(resolve)); await nextTick() } }
  const all = node => [node, ...node.children.flatMap(all)]
  const text = node => node.text + node.children.map(text).join('')
  const button = label => all(container).find(node => node.type === 'button' && text(node).includes(label))
  await settle()
  assert.ok(button('发起完整衔接审核'))
  assert.ok(button('转为协作需求'))
  button('转为协作需求').props.onClick()
  await nextTick()
  const target = all(container).find(node => node.props.id === 'linkage-request-target')
  assert.equal(target.options[target.selectedIndex]?.value, 'm1')
  const kind = all(container).find(node => node.props.id === 'linkage-request-kind')
  kind.props['onUpdate:modelValue']('person')
  await nextTick()
  const personTarget = all(container).find(node => node.props.id === 'linkage-request-target')
  assert.equal(personTarget.options[personTarget.selectedIndex]?.value, '')
  personTarget.props['onUpdate:modelValue']('u1')
  await nextTick()
  const form = all(container).find(node => node.type === 'form')
  form.props.onSubmit({ preventDefault() {} })
  await settle()
  const mutation = calls.find(call => call.path.endsWith('/findings/f1/request'))
  assert.equal(JSON.parse(mutation.options.body).target_module_id, null)
  assert.equal(JSON.parse(mutation.options.body).target_user_id, 'u1')
  assert.ok(button('已转为协作需求'))
  assert.ok(!button('转为协作需求')?.props.onClick || text(button('转为协作需求')).includes('已转为'))
  groups++
  // A maximally sized, valid model finding must still convert with the default form.
  saved = { ...saved, result: { ...saved.result, findings: [{ ...finding, problem: '问题'.repeat(2000), excerpt: '摘录'.repeat(2000), basis: '依据模块摘要。' + '上下文'.repeat(1333), suggestion: '修改'.repeat(2000) }] } }
  button('刷新报告').props.onClick()
  await settle()
  button('转为协作需求').props.onClick()
  await nextTick()
  const defaultContent = all(container).find(node => node.props.id === 'linkage-request-content').value
  assert.ok(defaultContent.length < 200, 'default dispatch instructions must leave room for the server-owned source')
  assert.ok(!defaultContent.includes('问题问题'))
  all(container).find(node => node.type === 'form').props.onSubmit({ preventDefault() {} })
  await settle()
  const longMutation = calls.filter(call => call.path.endsWith('/findings/f1/request')).at(-1)
  assert.equal(JSON.parse(longMutation.options.body).content, defaultContent)
  assert.equal(JSON.parse(longMutation.options.body).target_module_id, 'm1')
  assert.ok(button('已转为协作需求'))
  groups++
  app._instance.props.leader = false
  await nextTick()
  assert.ok(!button('发起完整衔接审核'))
  assert.ok(!button('确认并发起协作需求'))
  groups++
  saved = { ...saved, status: 'partial_failed', can_retry: true, can_convert: true, message: '审核未完成', error_code: 'transport_error' }
  button('刷新报告').props.onClick()
  await settle()
  assert.ok(!button('重试未完成审核'))
  app._instance.props.leader = true
  await nextTick()
  assert.ok(button('重试未完成审核'))
  button('重试未完成审核').props.onClick()
  await settle()
  assert.ok(calls.some(call => call.path.endsWith('/r1/retry') && call.options.method === 'POST'))
  assert.ok(!button('重试未完成审核'))
  groups++
  // A project change cancels the previous source; late responses cannot restore old data.
  app._instance.props.projectId = 'p2'
  await settle()
  assert.ok(cancelledSignal)
  app.unmount(); app = undefined
  assert.equal(cancelledSignal.aborted, true)
  assert.equal(listeners.size, 0)
  deferredResolve({ ok: true, status: 200, json: async () => [review] })
  await settle()
  assert.equal(container.children.length, 0)
  groups++
} finally {
  app?.unmount(); globalThis.fetch = originalFetch; globalThis.document = originalDocument; globalThis.Document = originalDocumentClass; globalThis.ShadowRoot = originalShadowRoot
  await rm(directory, { recursive: true, force: true })
}
console.log(`${groups} project linkage UI cases passed (fixed responses, no network)`)
