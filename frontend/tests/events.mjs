import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { readFile, mkdtemp, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { createRenderer, nextTick, h, ref } from 'vue'
import { parse, compileScript } from '@vue/compiler-sfc'
import { build } from 'esbuild'

const root = new URL('../', import.meta.url).pathname, require = createRequire(import.meta.url)
const directory = await mkdtemp(join(tmpdir(), 'teams-events-ui-'))
const globals = ['fetch', 'document', 'window', 'Document', 'ShadowRoot', 'setTimeout', 'clearTimeout']
const originals = Object.fromEntries(globals.map(key => [key, globalThis[key]]))
const clone = value => JSON.parse(JSON.stringify(value))
const workerNames = ['platform-review-worker', 'platform-project-linkage-worker', 'platform-plan-self-checker', 'platform-spec-drafter']
const event = (id, changes = {}) => ({ id, occurred_at: '2026-11-13T00:00:00Z', project_id: 'p1', actor_type: 'agent', actor_id: 'platform-review-worker', represented_user_id: null, event_type: 'review_report_accepted', object_type: 'delivery_version', object_id: 'd1', task_id: 'm1', plan_version_id: null, spec_version_id: null, details: {}, ...changes })
const initial = [
  ...workerNames.map((actor_id, index) => event(index + 1, { actor_id })),
  event(5, { actor_id: 'a1', represented_user_id: 'u1', event_type: 'progress_reported', details: { agent_name: '文献助手' } }),
  event(6, { actor_id: 'a2', represented_user_id: 'u1', event_type: 'progress_reported', details: { agent_name: '<script>助手</script>', execution_origin: 'platform_worker' } }),
  event(7, { actor_type: 'human', actor_id: 'u1', event_type: 'delivery_submitted' }),
]
let rows = clone(initial), calls = [], timerId = 0, app, groups = 0, hold = false, delayed, fail = false
const timers = new Set(), listeners = new Map()
globalThis.document = { visibilityState: 'visible', addEventListener: (key, fn) => { if (!listeners.has(key)) listeners.set(key, new Set()); listeners.get(key).add(fn) }, removeEventListener: (key, fn) => listeners.get(key)?.delete(fn) }
globalThis.window = { location: { search: '', origin: 'http://test.invalid' }, history: { replaceState() {} }, dispatchEvent() {} }
globalThis.Document = class {}; globalThis.ShadowRoot = class {}
globalThis.setTimeout = () => { const id = ++timerId; timers.add(id); return id }
globalThis.clearTimeout = id => timers.delete(id)
const response = (data, status = 200) => ({ ok: status === 200, status, json: async () => clone(data) })
globalThis.fetch = async (url, options) => {
  const path = String(url); calls.push({ path, options })
  if (path.includes('/events?')) {
    const query = new URL(path, 'http://test.invalid').searchParams
    const kind = query.get('actor_kind'), platform = row => row.actor_type === 'agent' && workerNames.includes(row.actor_id) && row.represented_user_id === null
    const filtered = rows.filter(row => row.project_id === path.split('/')[3])
      .filter(row => !kind || (kind === 'platform' ? platform(row) : kind === 'participant' ? !platform(row) : kind === 'human' ? row.actor_type === 'human' : row.actor_type === 'agent' && !platform(row)))
      .filter(row => !query.has('actor_id') || row.actor_id === query.get('actor_id'))
      .filter(row => !query.has('task_id') || row.task_id === query.get('task_id'))
      .filter(row => !query.has('object_id') || row.object_id === query.get('object_id'))
      .filter(row => !query.has('before_id') || row.id < Number(query.get('before_id')))
      .slice(-Number(query.get('limit') || 100))
    if (hold && kind === 'platform') return new Promise(resolve => { delayed = () => resolve(response(filtered)) })
    return response(fail ? { detail: '固定时间线失败' } : filtered, fail ? 503 : 200)
  }
  if (path === '/api/projects') return response([{ id: 'p1', name: '演示', role: 'leader', created_by: 'u1', created_at: '2026-11-13T00:00:00Z' }])
  if (path.endsWith('/members')) return response([{ user_id: 'u1', username: '负责人', role: 'leader' }])
  if (path.endsWith('/modules')) return response([{ id: 'm1', name: '文献与概念', goal: '核对概念', delivery_format: 'md', assignee_ids: ['u1'], upstream: [], deadline: null }])
  if (path.endsWith('/bottlenecks')) return response({ project_id: 'p1', evaluated_at: '2026-11-13T00:00:00Z', return_after_days: 3, items: [] })
  if (path.endsWith('/model-disclosure')) return response({ notice: { summary: '固定告知' }, project_consent: null, submission_acknowledged: false, can_consent: true })
  if (path.includes('/deliveries') || path.endsWith('/progress') || path.endsWith('/requests') || path.endsWith('/drafts') || path.includes('/notifications?')) return response([])
  throw new Error('Unexpected request ' + path)
}
const node = (type, text = '') => ({ type, text, props: {}, children: [], parent: null, selectedIndex: -1,
  addEventListener() {}, removeEventListener() {}, getRootNode() { return {} },
  get options() { return this.children.flatMap(child => child.type === 'option' ? [child] : child.options) },
})
const renderer = createRenderer({
  createElement: type => node(type), createText: text => node('#text', text), createComment: text => node('#comment', text),
  setText: (target, text) => { target.text = text }, setElementText: (target, text) => { target.text = text; target.children = [] },
  patchProp: (target, key, _old, value) => { target.props[key] = value; if (key === 'value' || key === 'multiple') target[key] = value }, parentNode: target => target.parent,
  nextSibling: target => target.parent?.children[target.parent.children.indexOf(target) + 1] ?? null,
  insert: (target, parent, anchor) => { if (target.parent) { const i = target.parent.children.indexOf(target); if (i >= 0) target.parent.children.splice(i, 1) } const i = anchor ? parent.children.indexOf(anchor) : -1; parent.children.splice(i < 0 ? parent.children.length : i, 0, target); target.parent = parent },
  remove: target => { if (target.parent) target.parent.children.splice(target.parent.children.indexOf(target), 1); target.parent = null },
})
const container = node('root'), all = target => [target, ...target.children.flatMap(all)], text = target => target.text + target.children.map(text).join('')
const byId = id => all(container).find(target => target.props.id === id)
const button = label => all(container).find(target => target.type === 'button' && text(target).includes(label))
const settle = async () => { for (let n = 0; n < 10; n++) { await new Promise(resolve => setImmediate(resolve)); await nextTick() } }
const eventCalls = () => calls.filter(call => call.path.includes('/events?'))
async function select(id, value) { const target = byId(id); assert.ok(target, id + ' is rendered'); target.props['onUpdate:modelValue'](value); await settle() }
function unmount() { app?.unmount(); app = undefined; assert.equal(container.children.length, 0); assert.equal(timers.size, 0) }
async function component(name) {
  const compiled = await build({ entryPoints: [join(root, 'src/components/' + name + '.vue')], bundle: true, platform: 'node', format: 'esm', write: false, plugins: [{ name: 'actual-vue', setup(builder) {
    builder.onResolve({ filter: /^vue$/ }, () => ({ path: require.resolve('vue'), external: true }))
    builder.onLoad({ filter: /\.vue$/ }, async ({ path }) => {
      const { descriptor } = parse(await readFile(path, 'utf8'), { filename: path })
      return { contents: compileScript(descriptor, { id: path, inlineTemplate: true }).content, loader: 'ts', resolveDir: join(path, '..') }
    })
  } }] })
  const bundle = join(directory, name + '.mjs'); await writeFile(bundle, compiled.outputFiles[0].text)
  return (await import(pathToFileURL(bundle).href)).default
}
try {
  const Workspace = await component('ProjectWorkspace')
  app = renderer.createApp(Workspace, { account: { id: 'u1', username: '负责人', is_admin: false }, busy: false }); app.mount(container); await settle()
  const query = new URL(eventCalls().at(-1).path, 'http://test.invalid').searchParams
  assert.equal(query.get('actor_kind'), 'participant', 'board excludes platform workers before pagination')
  assert.equal(query.get('limit'), '6')
  assert.match(text(container), /文献助手 代表 负责人/)
  assert.match(text(container), /负责人 · 提交交付/)
  assert.doesNotMatch(text(container), /平台审核后台|汇总审核报告/)
  groups++; unmount()

  const Timeline = await component('EventTimeline'), projectId = ref('p1')
  app = renderer.createApp({ setup: () => () => h(Timeline, { projectId: projectId.value, members: [{ user_id: 'u1', username: '负责人' }], modules: [{ id: 'm1', name: '文献与概念' }], deliveries: [{ id: 'd1', filename: '问题稿.md', version: 1 }], leader: true }) })
  app.mount(container); await settle()
  assert.equal(new URL(eventCalls().at(-1).path, 'http://test.invalid').searchParams.get('actor_kind'), null)
  const labels = all(container).filter(target => target.type === 'strong').map(text)
  for (const label of ['平台审核后台', '平台衔接审核后台', '平台方案自检', '平台规范起草', '智能体 文献助手 代表 负责人']) assert.ok(labels.includes(label), label)
  const platformRows = all(container).filter(target => target.type === 'li' && target.props['data-event-type'] === 'review_report_accepted')
  assert.equal(platformRows.length, 4)
  assert.ok(platformRows.every(target => !text(target).includes('代表的人') && !text(target).includes('未记录的所属人')))
  assert.match(text(container), /智能体 <script>助手<\/script> 代表 负责人/)
  assert.ok(!all(container).some(target => target.type === 'script' || target.props.innerHTML))
  groups++

  await select('timeline-kind', 'platform')
  assert.equal(new URL(eventCalls().at(-1).path, 'http://test.invalid').searchParams.get('actor_kind'), 'platform')
  assert.equal(all(container).filter(target => target.type === 'li').length, 4)
  assert.doesNotMatch(text(container), /智能体 文献助手 代表/)
  assert.ok(all(container).filter(target => target.type === 'a' && target.props.download !== undefined).every(target => !target.props.href.includes('actor_kind')))
  groups++

  await select('timeline-task', 'm1'); await select('timeline-delivery', 'd1'); await select('timeline-actor', 'platform-review-worker')
  const combined = new URL(eventCalls().at(-1).path, 'http://test.invalid').searchParams
  for (const [key, value] of Object.entries({ actor_kind: 'platform', task_id: 'm1', object_id: 'd1', actor_id: 'platform-review-worker' })) assert.equal(combined.get(key), value)
  await select('timeline-kind', 'agent')
  const userQuery = new URL(eventCalls().at(-1).path, 'http://test.invalid').searchParams
  assert.equal(userQuery.get('actor_id'), null, 'changing actor kind clears incompatible individual actor')
  assert.equal(all(container).filter(target => target.type === 'li').length, 2)
  assert.match(text(container), /智能体 文献助手 代表 负责人/)
  assert.doesNotMatch(text(container), /平台审核后台/)
  groups++

  await select('timeline-kind', 'human')
  assert.equal(all(container).filter(target => target.type === 'li').length, 1)
  assert.match(text(container), /提交正式交付/)
  groups++

  rows = Array.from({ length: 102 }, (_, index) => event(index + 10))
  await select('timeline-kind', 'platform')
  assert.ok(button('加载更早记录'))
  button('加载更早记录').props.onClick(); await settle()
  assert.equal(new URL(eventCalls().at(-1).path, 'http://test.invalid').searchParams.get('before_id'), '12')
  assert.equal(all(container).filter(target => target.type === 'li').length, 102)
  assert.equal(button('加载更早记录'), undefined)
  groups++

  rows = clone(initial); await select('timeline-kind', 'human')
  hold = true; await select('timeline-kind', 'platform'); assert.ok(delayed)
  await select('timeline-kind', 'human'); hold = false; delayed(); await settle()
  assert.equal(all(container).filter(target => target.type === 'li').length, 1)
  assert.doesNotMatch(text(container), /平台审核后台/)
  groups++

  fail = true; button('刷新时间线').props.onClick(); await settle()
  assert.match(text(container), /固定时间线失败/)
  fail = false; button('刷新时间线').props.onClick(); await settle()
  assert.doesNotMatch(text(container), /固定时间线失败/)
  assert.equal(new URL(eventCalls().at(-1).path, 'http://test.invalid').searchParams.get('actor_kind'), 'human')
  groups++

  projectId.value = 'p2'; await settle()
  const switched = new URL(eventCalls().at(-1).path, 'http://test.invalid')
  assert.ok(switched.pathname.includes('/p2/'))
  assert.equal(switched.searchParams.get('actor_kind'), null)
  assert.equal(switched.searchParams.get('task_id'), null)
  assert.equal(switched.searchParams.get('object_id'), null)
  assert.doesNotMatch(text(container), /文献助手/)
  assert.ok(eventCalls().every(call => call.options.method === 'GET' && call.options.cache === 'no-store'))
  groups++; unmount()
  assert.ok([...listeners.values()].every(set => set.size === 0))
} finally {
  app?.unmount()
  for (const [key, value] of Object.entries(originals)) globalThis[key] = value
  await rm(directory, { recursive: true, force: true })
}
console.log(`${groups} event actor/filter UI cases passed (actual Vue, fixed responses, no network)`)
