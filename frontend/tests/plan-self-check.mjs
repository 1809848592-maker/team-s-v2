import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { readFile, mkdtemp, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { createRenderer, nextTick, reactive } from 'vue'
import { parse, compileScript } from '@vue/compiler-sfc'
import { build } from 'esbuild'

const root = new URL('../', import.meta.url).pathname
const require = createRequire(import.meta.url)
const directory = await mkdtemp(join(tmpdir(), 'teams-plan-check-ui-'))
const originalFetch = globalThis.fetch, originalDocument = globalThis.document, originalDocumentClass = globalThis.Document, originalShadowRoot = globalThis.ShadowRoot
// The actual components use Vue form directives with an in-memory DOM boundary.
globalThis.document = {}
globalThis.Document = class {}
globalThis.ShadowRoot = class {}
let calls = [], deferred, pendingSignal, responseStatus = 200, delayed = false, consent = false, groups = 0
const clone = value => JSON.parse(JSON.stringify(value))
const content = { research_question: '研究问题', final_deliverable: '报告', argument: '主线', deadline: null, terms: [], data_definitions: [], modules: [] }
const notice = { version: 'test-notice', provider: 'DeepSeek', summary: '当前未发布方案和分工草稿将发送给DeepSeek', uncertainty: '数据使用条款', terms_url: 'https://example.invalid/terms', privacy_url: 'https://example.invalid/privacy', consent_required: true }
const result = { request_id: 'r1', status: 'completed', content_hash: 'a'.repeat(64), reviewer: 'plan_self_check', instruction_version: 'v1', model: 'fixed', input_hash: 'b'.repeat(64), discarded_findings: 1,
  findings: [{ check_key: 'dependency', severity: 'important', segment_id: 'S1', module_id: null, location: '整体方案', problem: '<script>问题</script>', excerpt: '<img src=x>', excerpt_verified: false, context_key: 'plan', context_excerpt: '主线', context_excerpt_verified: false, suggestion: '补全依赖', reviewer: 'plan_self_check', instruction_version: 'v1', model: 'fixed', input_hash: 'b'.repeat(64) }],
  checks: [{ key: 'dependency', status: 'issues', reason: '有意见' }, { key: 'alignment', status: 'uncovered', reason: '缺少主线' }], coverage_notes: ['有限材料'] }
globalThis.fetch = async (path, options) => {
  calls.push({ path: String(path), options })
  if (String(path).endsWith('/model-consent')) consent = true
  if (String(path).includes('/model-')) return { ok: true, status: 200, json: async () => ({ notice, project_consent: consent ? { accepted_at: '2026-11-12T00:00:00Z' } : null, can_consent: true }) }
  const response = { ok: responseStatus === 200, status: responseStatus, json: async () => responseStatus === 200 ? clone(result) : { detail: '方案自检未完成：固定网络失败' } }
  if (delayed) { pendingSignal = options.signal; return new Promise(resolve => { deferred = () => resolve(response) }) }
  return response
}
let app
try {
  async function loadComponent(filename) {
  const compiled = await build({ entryPoints: [join(root, 'src/components/' + filename + '.vue')], bundle: true, platform: 'node', format: 'esm', write: false, plugins: [{ name: 'vue-inline', setup(builder) {
    builder.onResolve({ filter: /^vue$/ }, () => ({ path: require.resolve('vue'), external: true }))
    builder.onLoad({ filter: /\.vue$/ }, async ({ path }) => {
      const { descriptor } = parse(await readFile(path, 'utf8'), { filename: path })
      const code = compileScript(descriptor, { id: path, inlineTemplate: true }).content
      return { contents: code, loader: 'ts', resolveDir: join(path, '..') }
    })
  } }] })
  const bundlePath = join(directory, filename + '.mjs')
  await writeFile(bundlePath, compiled.outputFiles[0].text)
  return (await import(pathToFileURL(bundlePath).href)).default
  }
  const Panel = await loadComponent('PlanSelfCheck')
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
  const props = reactive({ projectId: 'p1', accountId: 'u1', leader: true, content: clone(content) })
  app = renderer.createApp(Panel, props); app.mount(container)
  const settle = async () => { for (let n = 0; n < 8; n++) { await new Promise(resolve => setImmediate(resolve)); await nextTick() } }
  const all = node => [node, ...node.children.flatMap(all)]
  const text = node => node.text + node.children.map(text).join('')
  const button = label => all(container).find(node => node.type === 'button' && text(node).includes(label))
  await settle()
  assert.equal(button('方案自检').props.disabled, true)
  assert.match(text(container), /未发布方案/)
  const checkbox = all(container).find(node => node.type === 'input' && node.props.type === 'checkbox')
  checkbox.props['onUpdate:modelValue'](true)
  await nextTick()
  button('确认数据外发').props.onClick()
  await settle()
  assert.equal(button('方案自检').props.disabled, false)
  groups++
  const original = clone(props.content)
  button('方案自检').props.onClick()
  await settle()
  assert.deepEqual(props.content, original)
  const request = calls.find(c => c.path.endsWith('/self-check'))
  assert.deepEqual(JSON.parse(request.options.body).content, original)
  assert.match(text(container), /AI 生成，供参考/)
  assert.match(text(container), /仅供参考，不阻止发布/)
  assert.match(text(container), /摘录未能核对/)
  assert.match(text(container), /1 条意见因格式无效/)
  assert.match(text(container), /职责|依赖闭合/)
  assert.ok(!all(container).some(n => n.type === 'script' || n.type === 'img' || n.props.innerHTML))
  assert.match(text(container), /<script>问题<\/script>/)
  groups++
  props.content.argument = '已修改的主线'
  await settle()
  assert.match(text(container), /草稿已修改.*重新自检/)
  groups++
  responseStatus = 502
  button('方案自检').props.onClick()
  await settle()
  assert.match(text(container), /方案自检未完成/)
  assert.equal(props.content.argument, '已修改的主线')
  groups++
  responseStatus = 200; delayed = true
  button('方案自检').props.onClick()
  await settle()
  app._instance.props.accountId = 'u2'
  await settle()
  assert.equal(pendingSignal.aborted, true)
  deferred()
  await settle()
  assert.ok(!text(container).includes('<script>问题</script>'))
  groups++
  delayed = true
  button('方案自检').props.onClick()
  await settle()
  app._instance.props.projectId = 'p2'
  await settle()
  assert.equal(pendingSignal.aborted, true)
  deferred()
  await settle()
  assert.ok(!text(container).includes('<script>问题</script>'))
  groups++
  button('方案自检').props.onClick()
  await settle()
  app.unmount(); app = undefined
  assert.equal(pendingSignal.aborted, true)
  deferred()
  await settle()
  assert.equal(container.children.length, 0)
  groups++
  const Parent = await loadComponent('PlanPanel')
  for (const mode of ['skipped', 'failed', 'advice', 'pending']) {
    calls = []; pendingSignal = undefined; deferred = undefined
    const version = { id: 'v1', version: 1, published_by: 'u1', published_at: '2026-11-12T00:00:00Z', change_note: '旧方案', content: clone(content) }
    globalThis.fetch = async (path, options) => {
      path = String(path); calls.push({ path, options })
      let data = null
      if (path.endsWith('/plan/versions') && options.method === 'GET') data = [version]
      else if (path.endsWith('/model-disclosure')) data = { notice, project_consent: { accepted_at: '2026-11-12T00:00:00Z' }, can_consent: true }
      else if (path.endsWith('/plan/versions')) data = { ...version, id: 'v2', version: 2, ...JSON.parse(options.body) }
      else if (path.endsWith('/self-check')) {
        if (mode === 'pending') { pendingSignal = options.signal; return new Promise(resolve => { deferred = () => resolve({ ok: true, status: 200, json: async () => clone(result) }) }) }
        if (mode === 'failed') return { ok: false, status: 502, json: async () => ({ detail: '方案自检未完成' }) }
        data = clone(result)
      } else throw new Error('Unexpected request ' + path)
      return { ok: true, status: 200, json: async () => clone(data) }
    }
    app = renderer.createApp(Parent, { projectId: 'p1', accountId: 'u1', leader: true, members: [] }); app.mount(container)
    await settle()
    button('基于最新版本编辑').props.onClick()
    await settle()
    const question = all(container).find(n => n.type === 'textarea' && n.props.id.endsWith('-question'))
    question.props['onUpdate:modelValue']('当前编辑的草稿 ' + mode)
    const note = all(container).find(n => n.type === 'textarea' && n.props.id.endsWith('-change-note'))
    note.props['onUpdate:modelValue']('人工发布')
    await settle()
    if (mode !== 'skipped') {
      button('方案自检').props.onClick()
      await settle()
      const checked = calls.find(c => c.path.endsWith('/self-check'))
      assert.equal(JSON.parse(checked.options.body).content.research_question, '当前编辑的草稿 ' + mode)
      if (mode === 'failed') assert.match(text(container), /自检未完成/)
      if (mode === 'advice') assert.match(text(container), /重要意见/)
    }
    const publish = button('发布方案新版本')
    assert.ok(publish && !publish.props.disabled && !publish.parent.props.disabled)
    all(container).find(n => n.type === 'form').props.onSubmit({ preventDefault() {} })
    await settle()
    const published = calls.find(c => c.path.endsWith('/plan/versions') && c.options.method === 'POST')
    assert.equal(JSON.parse(published.options.body).content.research_question, '当前编辑的草稿 ' + mode)
    assert.match(text(container), /方案第 2 版已发布/)
    if (mode === 'pending') { assert.equal(pendingSignal.aborted, true); deferred(); await settle(); assert.ok(!text(container).includes('<script>问题</script>')) }
    app.unmount(); app = undefined
    groups++
  }
} finally {
  app?.unmount(); globalThis.fetch = originalFetch; globalThis.document = originalDocument; globalThis.Document = originalDocumentClass; globalThis.ShadowRoot = originalShadowRoot
  await rm(directory, { recursive: true, force: true })
}
console.log(`${groups} plan self-check UI cases passed (fixed responses, no network)`)
