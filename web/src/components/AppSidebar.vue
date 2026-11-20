<script setup>
import { useRoute } from 'vue-router'
import { computed } from 'vue'
import { useUiStore } from '@/stores/ui'
import { useLibraryStore } from '@/stores/library'
import { useJobsStore, useTasksStore } from '@/stores/quality'

const route = useRoute()
const ui = useUiStore()
const library = useLibraryStore()
const jobsStore = useJobsStore()
const tasksStore = useTasksStore()

const items = computed(() => [
  { id: 'overview', label: '概览', icon: 'grid', to: '/overview' },
  { id: 'library', label: '知识库', icon: 'book', to: '/library', count: library.documents.length },
  { id: 'search', label: '搜索', icon: 'search', to: '/search' },
  { id: 'agent', label: '测试台', icon: 'spark', to: '/agent' },
  { id: 'notes', label: '手记', icon: 'chat', to: '/notes' },
  { id: 'quality', label: '质量', icon: 'check', to: '/quality' },
  // 任务 = 活动层的 tasks 表（跨 agent 的工作单元）。
  // 解析队列 = 由文档状态派生的入库队列，是另一件事，分开导航。
  { id: 'tasks', label: '任务', icon: 'clock', to: '/tasks', count: tasksStore.counts.open },
  { id: 'jobs', label: '解析队列', icon: 'list', to: '/jobs', count: jobsStore.jobs.length },
  { id: 'llm', label: 'LLM 用量', icon: 'scale', to: '/llm' },
  // bench (对比) kept working but hidden from navigation; reachable at /#/bench.
  { id: 'settings', label: '设置', icon: 'gear', to: '/settings' },
])

const activeId = computed(() => {
  if (route.path.startsWith('/doc/')) return 'library'
  return route.path.replace('/', '') || 'overview'
})
</script>

<template>
  <aside class="sidebar" :class="{ collapsed: ui.sidebarCollapsed }">
    <div class="brand">
      <div class="brand-mark" aria-hidden="true">
        <svg viewBox="0 0 32 32"><rect width="32" height="32" rx="7"/><path d="M9 11h14M9 16h10M9 21h14"/></svg>
      </div>
      <div class="brand-copy">
        <h1>Toc Memex</h1>
        <p>AI交互档案库</p>
      </div>
    </div>

    <nav class="nav">
      <RouterLink
        v-for="item in items"
        :key="item.id"
        :to="item.to"
        class="nav-item"
        :class="{ active: activeId === item.id }"
        :title="item.label"
      >
        <svg v-if="item.icon === 'grid'" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg>
        <svg v-else-if="item.icon === 'book'" viewBox="0 0 24 24"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2Z"/></svg>
        <svg v-else-if="item.icon === 'search'" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
        <svg v-else-if="item.icon === 'spark'" viewBox="0 0 24 24"><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1"/><circle cx="12" cy="12" r="3.2"/></svg>
        <svg v-else-if="item.icon === 'chat'" viewBox="0 0 24 24"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2Z"/><path d="M8 9h8M8 13h5"/></svg>
        <svg v-else-if="item.icon === 'check'" viewBox="0 0 24 24"><path d="M20 13c0 5-3.5 7.5-8 9-4.5-1.5-8-4-8-9V5l8-3 8 3v8Z"/><path d="m9 12 2 2 4-4"/></svg>
        <svg v-else-if="item.icon === 'clock'" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>
        <svg v-else-if="item.icon === 'list'" viewBox="0 0 24 24"><path d="M8 6h13M8 12h13M8 18h13"/><circle cx="3.5" cy="6" r="1"/><circle cx="3.5" cy="12" r="1"/><circle cx="3.5" cy="18" r="1"/></svg>
        <svg v-else-if="item.icon === 'scale'" viewBox="0 0 24 24"><path d="M12 3v18"/><path d="M5 6h14"/><path d="m5 6-3 6a3.5 3.5 0 0 0 6 0L5 6Z"/><path d="m19 6-3 6a3.5 3.5 0 0 0 6 0l-3-6Z"/></svg>
        <svg v-else viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5V21h-4v-.1a1.7 1.7 0 0 0-1-1.5 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H3v-4h.1a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1a1.7 1.7 0 0 0 1.9.3 1.7 1.7 0 0 0 1-1.5V3h4v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.5 1h.1v4h-.1a1.7 1.7 0 0 0-1.5 1Z"/></svg>
        <span class="nav-label">{{ item.label }}</span>
        <span v-if="item.count" class="nav-count">{{ item.count }}</span>
      </RouterLink>
    </nav>

    <div class="sidebar-spacer"></div>
    <div class="sidebar-footer">
      <span>Toc Memex · 只读服务</span>
      <button class="theme-btn" title="切换主题" @click="ui.toggleTheme">
        <svg class="icon-sun" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
        <svg class="icon-moon" viewBox="0 0 24 24"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z"/></svg>
      </button>
    </div>
  </aside>
</template>
