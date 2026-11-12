<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api, dateText, errorText, isPlatformReviewWorker, projectPath, reviewJobEventLabels } from '../api/client'
import type { Member, Module, Delivery, ProjectEvent } from '../api/types'
const props = defineProps<{projectId:string;members:Member[];modules:Module[];deliveries:Delivery[];leader:boolean}>()
const events = ref<ProjectEvent[]>([])
const knownAgents = ref<Record<string,{id:string;name:string;representedUserId:string|null;platformWorker:boolean}>>({})
const task = ref(''), actor = ref(''), object = ref(''), error = ref(''), loading = ref(false), more = ref(false)
let serial = 0, mounted = true
const labels: Record<string,string> = {...reviewJobEventLabels,project_created:'创建项目',model_data_transfer_consented:'同意审核数据外发',submission_data_transfer_disclosed:'阅读首次交付外发告知',llm_project_quota_updated:'调整项目审核额度',invitation_created:'创建邀请',member_joined:'加入项目',plan_published:'发布整体方案',module_updated:'更新分工模块',spec_published:'发布团队规范',template_uploaded:'上传模板',delivery_submitted:'提交正式交付',delivery_resubmission_requested:'要求重新提交交付',delivery_review_started:'开始交付审核',delivery_review_state_changed:'更新交付审核状态',agent_registered:'登记智能体',agent_revoked:'吊销智能体令牌',agent_draft_uploaded:'上传交付稿',agent_draft_confirmed:'确认交付稿',agent_permission_denied:'智能体权限请求被拒绝',agent_tool_call:'智能体工具调用',agent_call:'智能体接口调用',agent_identity_read:'读取智能体身份',agent_tasks_read:'读取本人任务',agent_plan_read:'读取整体方案',agent_spec_read:'读取团队规范',agent_task_context_read:'读取任务上下文',agent_deliveries_read:'读取可见交付列表',agent_delivery_read:'读取交付',agent_review_read:'读取本人审核意见',agent_requests_read:'读取协作需求列表',agent_request_read:'读取协作需求',agent_resources_read:'读取资源列表',material_uploaded:'上传共享材料',progress_reported:'回报进度',request_created:'发起协作需求',request_accepted:'接手需求',request_responded:'回应需求',request_supplemented:'补充需求',request_resolved:'确认需求解决',notification_read:'阅读通知'}
function name(id:string) { return props.members.find(m=>m.user_id===id)?.username ?? id }
function agentName(event:ProjectEvent) { return isPlatformReviewWorker(event) ? '平台审核后台' : typeof event.details.agent_name==='string' && event.details.agent_name ? event.details.agent_name : event.actor_id }
function actorLabel(event:ProjectEvent) { return isPlatformReviewWorker(event) ? '平台审核后台' : event.actor_type==='agent' ? `智能体 ${agentName(event)} 代表 ${event.represented_user_id ? name(event.represented_user_id) : '未记录的所属人'}` : name(event.actor_id) }
const agentOptions = computed(()=>Object.values(knownAgents.value))
async function load(older = false) {
  const projectId = props.projectId
  const sequence = ++serial
  loading.value = true; error.value = ''
  if (!older) { events.value=[]; more.value=false }
  const query = new URLSearchParams({limit:'100'})
  if(task.value) query.set('task_id', task.value)
  if(actor.value) query.set('actor_id', actor.value)
  if(object.value) query.set('object_id', object.value)
  if(older && events.value[0]) query.set('before_id', String(events.value[0].id))
  try {
    const result = await api<ProjectEvent[]>(`${projectPath(projectId)}/events?${query}`)
    if(!mounted || projectId !== props.projectId || sequence !== serial) return
    for(const item of result) if(item.actor_type==='agent') knownAgents.value[item.actor_id] = {id:item.actor_id,name:agentName(item),representedUserId:item.represented_user_id,platformWorker:isPlatformReviewWorker(item)}
    events.value = older ? [...result, ...events.value] : result
    more.value = result.length === 100
  } catch(e) { if(mounted && projectId===props.projectId && sequence === serial) error.value = errorText(e) }
  finally { if(mounted && projectId===props.projectId && sequence === serial) loading.value = false }
}
watch([task, actor, object],()=>void load())
onMounted(()=>void load())
watch(()=>props.projectId,()=>{++serial;knownAgents.value={};task.value='';actor.value='';object.value='';void load()})
onBeforeUnmount(()=>{mounted=false;++serial})
</script>
<template>
  <section class="panel" aria-labelledby="timeline-title">
    <div class="panel-heading"><div><p class="eyebrow">操作留痕</p><h2 id="timeline-title">项目时间线</h2></div><button class="btn quiet" :disabled="loading" @click="load()">刷新时间线</button></div>
    <div class="form-grid timeline-filters">
      <div class="field"><label for="timeline-task">按任务查看</label><select id="timeline-task" v-model="task"><option value="">全部任务</option><option v-for="module in modules" :key="module.id" :value="module.id">{{module.name}}</option></select></div>
      <div class="field"><label for="timeline-actor">按操作者查看</label><select id="timeline-actor" v-model="actor"><option value="">全部成员与智能体</option><option v-for="member in members" :key="member.user_id" :value="member.user_id">{{member.username}}</option><option v-for="agent in agentOptions" :key="agent.id" :value="agent.id">{{agent.platformWorker ? '平台审核后台' : `智能体 ${agent.name} 代表 ${agent.representedUserId ? name(agent.representedUserId) : '未记录的所属人'}`}}</option></select></div>
      <div class="field"><label for="timeline-delivery">按交付查看</label><select id="timeline-delivery" v-model="object"><option value="">全部对象</option><option v-for="delivery in deliveries" :key="delivery.id" :value="delivery.id">{{delivery.filename}} · v{{delivery.version}}</option></select></div>
    </div>
    <div v-if="leader" class="toolbar export-actions"><a class="btn" :href="`${projectPath(projectId)}/events/export?format=json`" download>导出 JSON</a><a class="btn" :href="`${projectPath(projectId)}/events/export?format=csv`" download>导出 CSV</a><span class="muted">导出当前项目完整日志</span></div>
    <p v-if="error" class="error" role="alert">{{error}}</p><p v-if="loading" class="muted" role="status">正在读取记录…</p>
    <button v-if="more" class="btn quiet" :disabled="loading" @click="load(true)">加载更早记录</button>
    <p v-if="!events.length && !loading && !error" class="empty">暂无符合筛选条件的记录。</p>
    <ol class="timeline-list">
      <li v-for="event in [...events].reverse()" :key="event.id" :data-event-type="event.event_type">
        <div class="timeline-mark" aria-hidden="true"></div><div class="timeline-body"><div class="toolbar"><strong>{{actorLabel(event)}}</strong><span>{{labels[event.event_type] ?? event.event_type}}</span><time :datetime="event.occurred_at">{{dateText(event.occurred_at)}}</time></div>
        <p v-if="event.details.summary" class="pre-wrap">{{event.details.summary}}</p><p v-if="event.details.change_note" class="pre-wrap">{{event.details.change_note}}</p><p v-if="event.details.title">{{event.details.title}}</p>
        <details><summary>记录详情 · #{{event.id}}</summary><dl><dt>操作者类型</dt><dd>{{isPlatformReviewWorker(event) ? '平台审核后台' : event.actor_type==='agent' ? '智能体' : '本人'}}</dd><dt>操作者</dt><dd>{{event.actor_type==='agent' ? agentName(event) : name(event.actor_id)}}</dd><template v-if="event.actor_type==='agent' && !isPlatformReviewWorker(event)"><dt>代表的人</dt><dd>{{event.represented_user_id ? name(event.represented_user_id) : '未记录'}}</dd></template><dt>对象</dt><dd>{{event.object_type}} · {{event.object_id}}</dd><dt v-if="event.plan_version_id">方案版本</dt><dd v-if="event.plan_version_id">{{event.plan_version_id}}</dd><dt v-if="event.spec_version_id">规范版本</dt><dd v-if="event.spec_version_id">{{event.spec_version_id}}</dd></dl><pre>{{JSON.stringify(event.details,null,2)}}</pre></details>
        </div>
      </li>
    </ol>
  </section>
</template>
<style scoped>
.timeline-filters { margin: 24px 0; }.export-actions { margin-bottom: 24px; }.timeline-list{list-style:none;margin:24px 0 0;padding:0}.timeline-list li{display:grid;grid-template-columns:16px 1fr;gap:16px;position:relative;padding:0 0 28px}.timeline-list li:not(:last-child)::before{content:'';position:absolute;left:5px;top:14px;bottom:0;width:1px;background:var(--line)}.timeline-mark{width:11px;height:11px;background:var(--teal);border:3px solid #d9ede9;border-radius:50%;margin-top:5px}.timeline-body{min-width:0}.timeline-body time{font-size:12px;color:var(--muted);margin-left:auto}.timeline-body p{font-size:14px;margin:10px 0}.timeline-body details{margin-top:10px;font-size:12px;color:var(--muted)}.timeline-body summary{cursor:pointer}.timeline-body dl{display:grid;grid-template-columns:auto 1fr;gap:8px}.timeline-body dd{margin:0;overflow-wrap:anywhere}.timeline-body pre{max-width:100%;overflow:auto;background:var(--canvas);padding:12px}
</style>
