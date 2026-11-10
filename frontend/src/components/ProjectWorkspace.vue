<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import type { Account } from '../api/auth'
import { api, dateText, errorText, projectPath, requestLabels, stageLabels } from '../api/client'
import type { Project, Member, Module, ModuleProgress, Delivery, CollaborationRequest, Notification, ProjectEvent } from '../api/types'
import PlanPanel from './PlanPanel.vue'
import SpecPanel from './SpecPanel.vue'
import TaskPanel from './TaskPanel.vue'
import RequestPanel from './RequestPanel.vue'
import EventTimeline from './EventTimeline.vue'
const props = defineProps<{account:Account;busy:boolean;logoutError?:string}>()
defineEmits<{logout:[]}>()
const projects = ref<Project[]>([]), selected = ref(''), members = ref<Member[]>([]), modules = ref<Module[]>([])
const progress = ref<ModuleProgress[]>([]), deliveries = ref<Delivery[]>([]), requests = ref<CollaborationRequest[]>([]), events = ref<ProjectEvent[]>([]), notifications = ref<Notification[]>([])
const error = ref(''), notice = ref(''), loading = ref(true), acting = ref(false), projectName = ref('')
const inviteRole = ref<'member'|'leader'>('member'), inviteLink = ref(''), showMembers = ref(false), showNotifications = ref(false)
const inviteToken = ref(new URLSearchParams(window.location.search).get('invite') ?? '')
const tabs = [{key:'board',label:'项目看板'},{key:'plan',label:'方案与分工'},{key:'spec',label:'规范与模板'},{key:'tasks',label:'任务与交付'},{key:'requests',label:'协作需求'},{key:'timeline',label:'时间线'}] as const
const tab = ref<string>('board')
const preferredModule = ref('')
const current = computed(()=>projects.value.find(p=>p.id===selected.value))
const leader = computed(()=>current.value?.role==='leader')
const unread = computed(()=>notifications.value.filter(n=>!n.read_at))
const todo = computed(()=>requests.value.filter(r=>r.status==='responded' ? r.created_by===props.account.id : r.status==='accepted' ? r.accepted_by===props.account.id : r.status==='open' && (r.target_user_id===props.account.id || modules.value.some(m=>m.id===r.target_module_id && m.assignee_ids.includes(props.account.id)))))
let serial = 0
let loadedId = ''
function name(id:string) { return members.value.find(m=>m.user_id===id)?.username ?? id }
function stage(moduleId:string) { return progress.value.find(p=>p.module_id===moduleId) }
const activityLabels: Record<string,string> = {project_created:'创建项目',invitation_created:'创建邀请',member_joined:'加入项目',plan_published:'发布方案',module_updated:'更新分工',spec_published:'发布规范',template_uploaded:'上传模板',delivery_submitted:'提交交付',material_uploaded:'上传共享材料',progress_reported:'回报进度',request_created:'发起需求',request_accepted:'接手需求',request_responded:'回应需求',request_supplemented:'补充需求',request_resolved:'确认解决',notification_read:'阅读通知'}
async function loadProject() {
  const pid = selected.value, sequence = ++serial
  if(!pid) { loading.value = false; return }
  loading.value = true; error.value = ''; inviteLink.value = ''
  if (pid !== loadedId) { members.value=[]; modules.value=[]; progress.value=[]; deliveries.value=[]; requests.value=[]; events.value=[]; notifications.value=[] }
  const params = new URLSearchParams(window.location.search)
  if(!inviteToken.value) {params.set('project',pid); window.history.replaceState(null,'',`/?${params}`)}
  try {
    const base = projectPath(pid)
    const results = await Promise.all([
      api<Member[]>(`${base}/members`), api<Module[]>(`${base}/modules`), api<ModuleProgress[]>(`${base}/progress`),
      api<Delivery[]>(`${base}/deliveries`), api<CollaborationRequest[]>(`${base}/requests`), api<ProjectEvent[]>(`${base}/events?limit=100`), api<Notification[]>(`/api/notifications?project_id=${encodeURIComponent(pid)}`),
    ])
    if(sequence !== serial) return
    loadedId = pid
    ;[members.value, modules.value, progress.value, deliveries.value, requests.value, events.value, notifications.value] = results
  } catch(e) { if(sequence===serial) error.value = errorText(e) }
  finally { if(sequence===serial) loading.value = false }
}
async function loadProjects(preferred?:string) {
  try {
    projects.value = await api<Project[]>('/api/projects')
    const wanted = preferred ?? selected.value ?? ''
    const fromUrl = new URLSearchParams(window.location.search).get('project')
    selected.value = projects.value.find(p=>p.id===wanted)?.id ?? projects.value.find(p=>p.id===fromUrl)?.id ?? projects.value[0]?.id ?? ''
    await loadProject()
  } catch(e) {error.value=errorText(e);loading.value=false}
}
async function createProject() {
  if(acting.value) return
  acting.value=true;error.value='';notice.value=''
  try {const project=await api<Project>('/api/projects',{name:projectName.value});projectName.value='';tab.value='board';await loadProjects(project.id);notice.value='项目已创建。可以先邀请成员，再发布方案与分工。'}
  catch(e){error.value=errorText(e)}finally{acting.value=false}
}
async function createInvite() {
  if(acting.value) return
  acting.value=true;error.value='';inviteLink.value=''
  try {const result=await api<{join_path:string}>(`${projectPath(selected.value)}/invitations`,{role:inviteRole.value});inviteLink.value=new URL(result.join_path,window.location.origin).href;notice.value='一次性邀请已生成。请将链接交给受邀成员。'}
  catch(e){error.value=errorText(e)}finally{acting.value=false}
}
async function acceptInvite() {
  if(acting.value) return
  acting.value=true;error.value=''
  try {const result=await api<{project:Project}>('/api/invitations/accept',{token:inviteToken.value});inviteToken.value='';window.history.replaceState(null,'','/');await loadProjects(result.project.id);notice.value='已加入项目。'}
  catch(e){error.value=errorText(e)}finally{acting.value=false}
}
async function readNotification(notification:Notification) {
  if(acting.value) return
  acting.value=true;error.value=''
  try {await api(`/api/notifications/${notification.id}/read`,undefined,'POST');await loadProject()}
  catch(e){error.value=errorText(e)}finally{acting.value=false}
}
onMounted(()=>void loadProjects())
</script>
<template>
<div class="project-workspace">
  <header class="app-header"><a class="wordmark" href="/">Team.s<span>v2</span></a><span class="app-caption">科研协同工作台</span><div class="account-actions"><span>{{account.username}}</span><span v-if="account.is_admin" class="tag">平台管理员</span><button class="btn quiet" :disabled="busy" @click="$emit('logout')">退出登录</button></div></header>
  <div class="app-layout">
    <aside class="project-sidebar" aria-label="项目导航"><p class="eyebrow">研究项目</p><label for="project-select" class="sr-only">切换项目</label><select id="project-select" v-model="selected" :disabled="loading || acting" @change="tab='board';loadProject()"><option v-if="!projects.length" value="">暂无项目</option><option v-for="project in projects" :key="project.id" :value="project.id">{{project.name}}</option></select>
      <nav v-if="current" aria-label="项目页面"><button v-for="item in tabs" :key="item.key" class="nav-button" :aria-current="tab===item.key?'page':undefined" @click="tab=item.key">{{item.label}}<span v-if="item.key==='requests' && todo.length" class="nav-count">{{todo.length}}</span></button></nav>
      <form class="new-project-form" @submit.prevent="createProject"><label for="new-project-name">新建项目</label><input id="new-project-name" v-model="projectName" required maxlength="120" placeholder="例如：城市语言景观研究"><button class="btn" :disabled="acting || !projectName.trim()">创建项目</button></form>
      <p class="sidebar-note">人负责验证，<br>协作留下记录。</p>
    </aside>
    <main class="project-main">
      <p v-if="logoutError" class="error" role="alert">{{logoutError}}</p>
      <section v-if="inviteToken" class="invitation-banner"><div><strong>你收到了一份项目邀请</strong><p>以 {{account.username}} 的身份加入受邀项目。</p></div><button class="btn primary" :disabled="acting" @click="acceptInvite">接受邀请并加入</button></section>
      <p v-if="error" class="error status-banner" role="alert">{{error}} <button class="btn quiet" :disabled="loading" @click="loadProjects()">重新加载</button></p><p v-if="notice" class="notice" role="status">{{notice}}</p>
      <template v-if="current">
        <div class="project-heading"><div><p class="eyebrow">{{leader?'项目负责人':'项目成员'}}</p><h1>{{current.name}}</h1></div><div class="toolbar"><button class="btn quiet" :disabled="loading" @click="loadProject">刷新</button><button class="btn" :aria-expanded="showMembers" @click="showMembers=!showMembers">成员与邀请 · {{members.length}}</button><button class="btn" :aria-expanded="showNotifications" @click="showNotifications=!showNotifications">站内通知{{unread.length?` · ${unread.length} 未读`:''}}</button></div></div>
        <section v-if="showMembers" class="panel members-panel" aria-labelledby="members-title"><div class="panel-heading"><h2 id="members-title">项目成员</h2><button class="btn quiet" @click="showMembers=false">收起成员</button></div><ul class="member-list"><li v-for="member in members" :key="member.user_id"><span>{{member.username}}</span><span class="tag">{{member.role==='leader'?'负责人':'组员'}}</span></li></ul><form v-if="leader" class="toolbar invite-form" @submit.prevent="createInvite"><div class="field"><label for="invite-role">邀请角色</label><select id="invite-role" v-model="inviteRole"><option value="member">组员</option><option value="leader">负责人</option></select></div><button class="btn primary" :disabled="acting">生成一次性邀请</button></form><div v-if="inviteLink" class="field invite-result"><label for="invite-link">邀请链接（单次使用）</label><input id="invite-link" :value="inviteLink" readonly @focus="($event.target as HTMLInputElement).select()"><span class="muted">复制链接后发送给受邀成员。每邀请一人生成一个新链接。</span></div></section>
        <section v-if="showNotifications" class="panel" aria-labelledby="notifications-title"><div class="panel-heading"><h2 id="notifications-title">站内通知</h2><button class="btn quiet" @click="showNotifications=false">收起通知</button></div><p v-if="!notifications.length" class="empty">暂时没有通知。</p><ul class="notification-list"><li v-for="notification in notifications" :key="notification.id" :class="{unread:!notification.read_at}"><div><p>{{notification.message}}</p><time>{{dateText(notification.created_at)}}</time></div><button v-if="!notification.read_at" class="btn quiet" :disabled="acting" @click="readNotification(notification)">标为已读</button><span v-else class="muted">已读</span></li></ul></section>
        <p v-if="loading" class="muted" role="status">正在同步项目记录…</p>
        <template v-if="tab==='board'">
          <section class="board-intro"><div><p class="eyebrow">协作现场</p><h2>让研究沿着清楚的分工推进。</h2><p class="muted">以交付记录进展，以回应完成交接。</p></div><div class="board-count"><strong>{{modules.length}}</strong><span>个分工模块</span></div></section>
          <div v-if="!modules.length && !loading" class="panel empty"><h3>从共同的方案开始</h3><p>{{leader?'先邀请组员，再到“方案与分工”发布研究目标和模块。':'负责人发布方案后，分工将在这里显示。'}}</p><button v-if="leader" class="btn primary" @click="tab='plan'">制定方案与分工</button></div>
          <section v-else class="module-grid" aria-label="分工模块看板"><article v-for="module in modules" :key="module.id" class="module-card"><div class="toolbar"><span class="module-format">{{module.delivery_format.toUpperCase()}}</span><span class="tag" :class="{submitted:stage(module.id)?.stage==='submitted'}">{{stageLabels[stage(module.id)?.stage ?? 'not_started']}}</span></div><h3>{{module.name}}</h3><p class="module-goal">{{module.goal}}</p><p class="muted">负责人 · {{module.assignee_ids.map(name).join('、')}}</p><p class="muted">截止 · {{dateText(module.deadline)}}</p><div class="last-record"><template v-if="stage(module.id)?.last_report"><p>{{stage(module.id)?.last_report?.summary}}</p><span>本人记录 · {{name(stage(module.id)!.last_report!.user_id)}} · {{dateText(stage(module.id)?.last_report?.created_at)}}</span></template><p v-else class="muted">尚无进度回报</p></div><p class="muted">交付 · {{stage(module.id)?.delivery_id?'已有正式提交':'尚未提交'}}</p><button class="btn quiet" @click="preferredModule=module.id;tab='tasks'">查看任务与交付 →</button></article></section>
          <div class="board-bottom"><section class="panel"><div class="panel-heading"><h2>待我处理</h2><button class="btn quiet" @click="tab='requests'">查看协作需求</button></div><p v-if="!todo.length" class="empty">当前没有待你处理的协作需求。</p><ul class="compact-list"><li v-for="request in todo.slice(0,8)" :key="request.id"><button class="text-button" @click="tab='requests'">{{request.title}}</button><span class="tag">{{requestLabels[request.status]}}</span></li></ul></section><section class="panel"><div class="panel-heading"><h2>最近活动</h2><button class="btn quiet" @click="tab='timeline'">完整时间线</button></div><ul class="compact-list"><li v-for="event in events.slice(-6).reverse()" :key="event.id"><span>{{name(event.actor_id)}} · {{activityLabels[event.event_type] ?? event.event_type}}</span><time>{{dateText(event.occurred_at)}}</time></li></ul></section></div>
        </template>
        <PlanPanel v-if="tab==='plan'" :key="selected" :project-id="selected" :members="members" :leader="leader" @changed="loadProject"/>
        <SpecPanel v-if="tab==='spec'" :key="selected" :project-id="selected" :leader="leader" @changed="loadProject"/>
        <TaskPanel v-if="tab==='tasks'" :initial-module-id="preferredModule" :key="selected" :project-id="selected" :members="members" :account-id="account.id" @changed="loadProject"/>
        <RequestPanel v-if="tab==='requests'" :key="selected" :project-id="selected" :members="members" :account-id="account.id" @changed="loadProject"/>
        <EventTimeline v-if="tab==='timeline'" :key="selected" :project-id="selected" :members="members" :modules="modules" :deliveries="deliveries" :leader="leader"/>
      </template>
      <section v-else-if="!loading" class="panel welcome-panel"><p class="eyebrow">你的协作空间</p><h1>开始一个研究项目。</h1><p>从左侧创建项目，或使用负责人发来的邀请链接加入团队。</p></section>
    </main>
  </div>
</div>
</template>
