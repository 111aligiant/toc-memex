import { createRouter, createWebHashHistory } from 'vue-router'

const routes = [
  { path: '/', redirect: '/overview' },
  { path: '/overview', component: () => import('@/views/OverviewView.vue'), meta: { title: '概览' } },
  { path: '/library', component: () => import('@/views/LibraryView.vue'), meta: { title: '知识库' } },
  {
    path: '/doc/:id',
    component: () => import('@/views/DocumentView.vue'),
    meta: { title: '文档' },
  },
  { path: '/search', component: () => import('@/views/SearchView.vue'), meta: { title: '搜索' } },
  { path: '/agent', component: () => import('@/views/AgentView.vue'), meta: { title: '测试台' } },
  { path: '/notes', component: () => import('@/views/NotesView.vue'), meta: { title: '手记' } },
  { path: '/quality', component: () => import('@/views/QualityView.vue'), meta: { title: '质量' } },
  { path: '/tasks', component: () => import('@/views/TasksView.vue'), meta: { title: '任务' } },
  { path: '/jobs', component: () => import('@/views/JobsView.vue'), meta: { title: '解析队列' } },
  { path: '/llm', component: () => import('@/views/LlmView.vue'), meta: { title: 'LLM 用量' } },
  { path: '/bench', component: () => import('@/views/BenchView.vue'), meta: { title: '对比' } },
  { path: '/settings', component: () => import('@/views/SettingsView.vue'), meta: { title: '设置' } },
  { path: '/ui-kit', component: () => import('@/views/UiKitView.vue'), meta: { title: 'UI 组件' } },
  { path: '/:pathMatch(.*)*', redirect: '/overview' },
]

export const router = createRouter({
  history: createWebHashHistory(),
  routes,
})
