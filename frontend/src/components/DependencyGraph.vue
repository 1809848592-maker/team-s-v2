<script setup lang="ts">
import { computed, useId } from 'vue'
import type { Module } from '../api/types'

const props = defineProps<{ modules: Module[] }>()
const graphId = `dependencies-${useId().replace(/:/g, '')}`
const graph = computed(() => {
  const byId = new Map(props.modules.map(module => [module.id, module]))
  const depths = new Map<string, number>()
  let cycle = false
  function depth(id: string, path = new Set<string>()): number {
    if (path.has(id)) { cycle = true; return 0 }
    const known = depths.get(id)
    if (known !== undefined) return known
    const module = byId.get(id)
    const next = new Set([...path, id])
    const parents = module?.upstream.filter(item => byId.has(item.module_id)) ?? []
    const value = parents.length ? Math.max(...parents.map(item => depth(item.module_id, next))) + 1 : 0
    depths.set(id, value)
    return value
  }
  const rows = new Map<number, number>()
  const nodes = props.modules.map(module => {
    const layer = depth(module.id)
    const row = rows.get(layer) ?? 0
    rows.set(layer, row + 1)
    return { module, x: 24 + layer * 248, y: 24 + row * 100 }
  })
  const positions = new Map(nodes.map(node => [node.module.id, node]))
  const edges = nodes.flatMap(node => node.module.upstream.flatMap((dependency, index) => {
    const upstream = positions.get(dependency.module_id)
    if (!upstream) return []
    const startX = upstream.x + 184
    const startY = upstream.y + 32
    const endX = node.x
    const endY = node.y + 32
    const bendX = (startX + endX) / 2
    return [{
      id: `${node.module.id}-${index}`,
      from: upstream.module.name || '未命名模块', to: node.module.name || '未命名模块',
      needs: dependency.needs,
      path: `M ${startX} ${startY} C ${bendX} ${startY}, ${bendX} ${endY}, ${endX} ${endY}`,
    }]
  }))
  return {
    nodes, edges, cycle,
    width: Math.max(420, ...nodes.map(node => node.x + 208)),
    height: Math.max(130, ...nodes.map(node => node.y + 88)),
  }
})
function shortName(value: string): string { return value.length > 12 ? `${value.slice(0, 11)}…` : value || '未命名模块' }
</script>

<template>
  <div class="dependency-graph">
    <p v-if="!modules.length" class="empty">添加分工模块后，这里会显示模块间的依赖。</p>
    <template v-else>
      <p v-if="graph.cycle" class="error" role="alert">当前依赖存在环，请调整上游模块后再发布。</p>
      <p class="muted">箭头从上游指向接收内容的模块；具体交接内容列在图下。</p>
      <div class="graph-scroll" tabindex="0" aria-label="模块依赖关系图，可横向滚动">
        <svg :viewBox="`0 0 ${graph.width} ${graph.height}`" :width="graph.width" :height="graph.height" role="img" :aria-labelledby="`${graphId}-title ${graphId}-description`">
          <title :id="`${graphId}-title`">模块依赖关系</title>
          <desc :id="`${graphId}-description`">共 {{ modules.length }} 个模块、{{ graph.edges.length }} 条依赖。下方列表提供每条依赖的完整文字。</desc>
          <defs>
            <marker :id="`${graphId}-arrow`" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto" markerUnits="userSpaceOnUse">
              <path d="M 0 0 L 8 4 L 0 8 Z" fill="#28746c" />
            </marker>
          </defs>
          <path v-for="edge in graph.edges" :key="edge.id" :d="edge.path" fill="none" stroke="#28746c" stroke-width="1.7" :marker-end="`url(#${graphId}-arrow)`">
            <title>{{ edge.from }} → {{ edge.to }}：{{ edge.needs || '待填写所需内容' }}</title>
          </path>
          <g v-for="node in graph.nodes" :key="node.module.id" :transform="`translate(${node.x},${node.y})`">
            <title>{{ node.module.name || '未命名模块' }}</title>
            <rect width="184" height="64" rx="6" fill="#ffffff" stroke="#b7c9d4" />
            <rect width="4" height="64" rx="2" fill="#28746c" />
            <text x="16" y="27" fill="#263c56" font-size="13" font-weight="600">{{ shortName(node.module.name) }}</text>
            <text x="16" y="47" fill="#627184" font-size="11">{{ node.module.upstream.length ? `${node.module.upstream.length} 个上游` : '无上游依赖' }}</text>
          </g>
        </svg>
      </div>
      <ul v-if="graph.edges.length" class="dependency-details">
        <li v-for="edge in graph.edges" :key="edge.id">
          <strong>{{ edge.from }} <span aria-hidden="true">→</span> {{ edge.to }}</strong>
          <p>{{ edge.needs || '待填写所需内容' }}</p>
        </li>
      </ul>
      <p v-else class="muted">各模块当前独立开展，没有设置上游依赖。</p>
    </template>
  </div>
</template>

<style scoped>
.graph-scroll { overflow: auto; max-height: 540px; border: 1px solid #dce3eb; border-radius: 6px; background: #f5f8fa; }
.graph-scroll svg { display: block; max-width: none; font-family: inherit; }
.dependency-details { margin: 16px 0 0; padding: 0; list-style: none; display: grid; gap: 12px; }
.dependency-details li { padding: 0 0 0 12px; border-left: 2px solid #b7cfca; font-size: 13px; overflow-wrap: anywhere; }
.dependency-details p { margin: 6px 0 0; color: #627184; line-height: 1.7; white-space: pre-wrap; }
</style>
