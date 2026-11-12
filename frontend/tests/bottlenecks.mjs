import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { readFile, mkdtemp, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { createRenderer, nextTick } from 'vue'
import { parse, compileScript } from '@vue/compiler-sfc'
import { build } from 'esbuild'

const root = new URL('../', import.meta.url).pathname, require = createRequire(import.meta.url)
const directory = await mkdtemp(join(tmpdir(), 'teams-bottlenecks-ui-'))
const originals = Object.fromEntries(['fetch', 'document', 'window', 'Document', 'ShadowRoot', 'setTimeout', 'clearTimeout'].map(key => [key, globalThis[key]]))
const clone = value => JSON.parse(JSON.stringify(value))
const listeners = new Map(), timers = new Map()
let timerId = 0, calls = [], delayed = [], mode = 'normal', responseStatus = 200, groups = 0, app
const modules = [
  { id: 'm1', name: '文献综述', assignee_ids: ['u1'], deadline: '2026-11-01T00:00:00Z', goal: '梳理证据', delivery_format: 'md', upstream: [] },
  { id: 'm2', name: '研究方法', assignee_ids: ['u2'], deadline: null, goal: '核对方法', delivery_format: 'md', upstream: [] },
]
const member = { user_id: 'u1', username: '负责人', role: 'leader' }
const project = (id, role = 'leader') => ({ id, name: '项目' + id, role, created_by: 'u1', created_at: '2026-11-01T00:00:00Z' })
const alert = (kind, changes = {}) => ({ kind, module_id: 'm1', delivery_id: null, request_id: null, title: '文献综述', since: '2026-11-01T00:00:00Z', ...changes })
let items = [alert('submission_overdue'), alert('resubmission_overdue', { module_id: 'm2', title: '研究方法', delivery_id: 'd1' }), alert('request_overdue', { module_id: null, request_id: 'r1', title: '<script>补充资料</script>' })]
const result = pid => ({ project_id: pid, evaluated_at: '2026-11-12T00:00:00Z', return_after_days: 3, items: clone(items) })
const requestRows = ['r1', 'r2'].map(id => ({ id, project_id: 'p1', title: '协作需求' + id, content: '请核对资料', created_by: 'u1', target_user_id: 'u2', target_module_id: null, expected_by: null, status: 'open', accepted_by: null, created_at: '2026-11-01T00:00:00Z', messages: [] }))
globalThis.document = { visibilityState: 'visible', addEventListener: (name, fn) => { if (!listeners.has(name)) listeners.set(name, new Set()); listeners.get(name).add(fn) }, removeEventListener: (name, fn) => listeners.get(name)?.delete(fn) }
globalThis.window = { location: { search: '', origin: 'http://test.invalid' }, history: { replaceState() {} }, dispatchEvent() {} }
globalThis.Document = class {}; globalThis.ShadowRoot = class {}
globalThis.setTimeout = (fn, delay) => { const id = ++timerId; timers.set(id, { fn, delay }); return id }
globalThis.clearTimeout = id => timers.delete(id)
const ok = (data, status = 200) => ({ ok: status === 200, status, json: async () => clone(data) })
globalThis.fetch = async (url, options) => {
  const path = String(url); calls.push({ path, options })
  if (path.endsWith('/bottlenecks')) {
    const pid = path.split('/')[3], data = responseStatus === 200 ? result(mode === 'wrong-project' ? 'p9' : pid) : { detail: '固定读取失败' }, status = responseStatus
    if (mode === 'delay') return new Promise(resolve => delayed.push({ signal: options.signal, resolve: () => resolve(ok(data, status)) }))
    return ok(data, status)
  }
  if (path === '/api/projects') return ok([project('p1', mode === 'member' ? 'member' : 'leader'), project('p2')])
  if (path.endsWith('/model-disclosure')) {
    const notice = { version: 'test', provider: 'DeepSeek', summary: '数据外发', terms_url: '', privacy_url: '', uncertainty: '', consent_required: true }
    return ok(path === '/api/model-disclosure' ? notice : { notice, project_consent: null, submission_acknowledged: false, can_consent: true })
  }
  if (path.endsWith('/members')) return ok([member])
  if (path.endsWith('/modules')) return ok(modules)
  if (/\/modules\/[^/]+\/progress$/.test(path)) return ok([])
  if (path.endsWith('/progress')) return ok(modules.map(m => ({ module_id: m.id, stage: 'not_started', last_report: null, delivery_id: null })))
  if (path.endsWith('/requests')) return ok(requestRows)
  if (/\/requests\/r[12]$/.test(path)) return ok(requestRows.find(row => path.endsWith('/' + row.id)))
  if (path.includes('/deliveries') || path.includes('/drafts') || path.endsWith('/materials') || path.includes('/requests') || path.includes('/events?') || path.includes('/notifications?')) return ok([])
  throw new Error('Unexpected request ' + path)
}
const makeNode = (type, text = '') => ({
  type, text, children: [], props: {}, parent: null, listeners: new Map(), selectedIndex: -1,
  addEventListener(name, handler) { this.listeners.set(name, handler) }, removeEventListener(name) { this.listeners.delete(name) }, getRootNode() { return {} },
  get options() { return this.children.flatMap(child => child.type === 'option' ? [child] : child.options) },
})
const renderer = createRenderer({
  createElement: type => makeNode(type), createText: text => makeNode('#text', text), createComment: text => makeNode('#comment', text),
  setText: (node, text) => { node.text = text }, setElementText: (node, text) => { node.text = text; node.children = [] },
  patchProp: (node, key, _old, value) => { node.props[key] = value; if (key === 'value' || key === 'multiple') node[key] = value }, parentNode: node => node.parent,
  nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
  insert: (node, parent, anchor) => { if (node.parent) { const i = node.parent.children.indexOf(node); if (i >= 0) node.parent.children.splice(i, 1) } const i = anchor ? parent.children.indexOf(anchor) : -1; parent.children.splice(i < 0 ? parent.children.length : i, 0, node); node.parent = parent },
  remove: node => { if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1); node.parent = null },
})
const container = makeNode('root'), all = node => [node, ...node.children.flatMap(all)], text = node => node.text + node.children.map(text).join('')
const button = label => all(container).find(node => node.type === 'button' && text(node).includes(label))
const cards = () => all(container).filter(node => node.type === 'article' && String(node.props.class).includes('module-card'))
const alertCalls = () => calls.filter(c => c.path.endsWith('/bottlenecks'))
const settle = async () => { for (let n = 0; n < 10; n++) { await new Promise(resolve => setImmediate(resolve)); await nextTick() } }
async function tick(delay) { const entry = [...timers].find(([, timer]) => timer.delay === delay); assert.ok(entry, 'scheduled timer ' + delay); timers.delete(entry[0]); entry[1].fn(); await settle() }
async function visible(value) { document.visibilityState = value; for (const fn of listeners.get('visibilitychange') ?? []) fn(); await settle() }
try {
  const compiled = await build({ entryPoints: [join(root, 'src/components/ProjectWorkspace.vue')], bundle: true, platform: 'node', format: 'esm', write: false, plugins: [{ name: 'actual-vue', setup(builder) {
    builder.onResolve({ filter: /^vue$/ }, () => ({ path: require.resolve('vue'), external: true }))
    builder.onLoad({ filter: /\.vue$/ }, async ({ path }) => {
      const { descriptor } = parse(await readFile(path, 'utf8'), { filename: path })
      return { contents: compileScript(descriptor, { id: path, inlineTemplate: true }).content, loader: 'ts', resolveDir: join(path, '..') }
    })
  } }] })
  const bundlePath = join(directory, 'workspace.mjs'); await writeFile(bundlePath, compiled.outputFiles[0].text)
  const Workspace = (await import(pathToFileURL(bundlePath).href)).default
  async function mount() { app = renderer.createApp(Workspace, { account: { id: 'u1', username: '负责人', is_admin: false }, busy: false }); app.mount(container); await settle() }
  function unmount() { app?.unmount(); app = undefined; assert.equal(container.children.length, 0); assert.equal(timers.size, 0) }
  await mount()
  assert.equal(alertCalls().length, 1)
  assert.ok(cards().every(card => String(card.props.class).includes('bottleneck')))
  assert.match(text(container), /超期未提交/)
  assert.match(text(container), /退回超过 3 天未重新提交/)
  assert.match(text(container), /需求回应超时/)
  assert.match(text(container), /<script>补充资料<\/script>/)
  assert.ok(!all(container).some(node => node.type === 'script' || node.props.innerHTML))
  assert.ok(alertCalls().every(c => c.options.method === 'GET' && c.options.cache === 'no-store'))
  groups++

  items = []
  await tick(60000)
  assert.ok(cards().every(card => !String(card.props.class).includes('bottleneck')))
  assert.match(text(container), /当前没有超时卡点/)
  items = [alert('request_overdue', { request_id: 'r2', module_id: 'm2' })]
  await tick(60000)
  assert.ok(!String(cards()[0].props.class).includes('bottleneck'))
  assert.ok(String(cards()[1].props.class).includes('bottleneck'))
  groups++

  responseStatus = 503
  await tick(60000)
  assert.match(text(container), /卡点读取失败.*固定读取失败/)
  assert.doesNotMatch(text(container), /当前没有超时卡点/)
  responseStatus = 200
  button('重试读取卡点').props.onClick()
  await settle()
  assert.doesNotMatch(text(container), /卡点读取失败/)
  groups++

  responseStatus = 403
  await tick(60000)
  assert.match(text(container), /卡点读取失败/)
  assert.equal(timers.size, 0, 'access denial stops automatic reads')
  assert.ok(cards().every(card => !String(card.props.class).includes('bottleneck')))
  responseStatus = 200
  button('重试读取卡点').props.onClick(); await settle()
  groups++

  mode = 'wrong-project'
  await tick(60000)
  assert.match(text(container), /卡点记录与当前项目不一致/)
  assert.doesNotMatch(text(container), /当前没有超时卡点/)
  mode = 'normal'
  button('重试读取卡点').props.onClick(); await settle()
  groups++

  const count = alertCalls().length
  await visible('hidden')
  assert.equal(timers.size, 0)
  await visible('visible')
  assert.equal(alertCalls().length, count + 1)
  groups++

  mode = 'delay'
  await tick(60000)
  await tick(15000)
  assert.equal(delayed.at(-1).signal.aborted, true)
  delayed.shift().resolve(); await settle()
  assert.match(text(container), /读取卡点超时/)
  groups++

  // Late p1 response cannot repaint p2 or leave old module alerts behind.
  button('重试读取卡点').props.onClick(); await settle()
  const old = delayed.shift()
  const select = all(container).find(node => node.props.id === 'project-select')
  select.props['onUpdate:modelValue']('p2'); select.props.onChange(); await settle()
  assert.equal(old.signal.aborted, true)
  old.resolve(); await settle()
  assert.doesNotMatch(text(container), /需求回应超时/)
  mode = 'normal'; for (const pending of delayed.splice(0)) pending.resolve(); await settle()
  assert.ok(alertCalls().at(-1).path.includes('/p2/'))
  groups++

  mode = 'delay'; await tick(60000)
  const oldAccount = delayed.shift()
  app._instance.props.account = { id: 'u2', username: '另一人', is_admin: false }; await settle()
  assert.equal(oldAccount.signal.aborted, true)
  oldAccount.resolve(); await settle()
  assert.doesNotMatch(text(container), /需求回应超时/)
  for (const pending of delayed.splice(0)) pending.resolve(); await settle()
  mode = 'normal'
  groups++

  button('处理需求').props.onClick(); await settle()
  assert.ok(button('刷新需求'))
  assert.ok(calls.at(-1).path.endsWith('/requests/r2'), 'opens the flagged request, even when it is not the first row')
  assert.equal(timers.size, 0)
  button('项目看板').props.onClick(); await settle()
  items = [alert('submission_overdue')]; await tick(60000)
  button('处理交付').props.onClick(); await settle()
  assert.ok(button('刷新任务'))
  assert.ok(calls.some(c => c.path.includes('/modules/m1/progress')))
  groups++
  unmount()

  mode = 'member'; calls = []
  await mount()
  assert.equal(alertCalls().length, 0)
  assert.doesNotMatch(text(container), /卡点/)
  assert.ok(cards().every(card => !String(card.props.class).includes('bottleneck')))
  unmount(); groups++

  mode = 'delay'; calls = []; await mount()
  const oldUnmount = delayed.shift(); unmount()
  assert.equal(oldUnmount.signal.aborted, true)
  oldUnmount.resolve(); await settle(); assert.equal(container.children.length, 0)
  assert.ok([...listeners.values()].every(set => set.size === 0))
  groups++
} finally {
  app?.unmount()
  for (const [key, value] of Object.entries(originals)) globalThis[key] = value
  await rm(directory, { recursive: true, force: true })
}
console.log(`${groups} bottleneck board UI cases passed (actual Vue, fixed responses, no network)`)
