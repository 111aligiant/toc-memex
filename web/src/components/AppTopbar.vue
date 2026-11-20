<script setup>
import { onMounted, onUnmounted, useTemplateRef } from 'vue'
import { useRouter } from 'vue-router'
import { useUiStore } from '@/stores/ui'

defineProps({
  title: { type: String, required: true },
})

const router = useRouter()
const ui = useUiStore()
const searchInput = useTemplateRef('global-search')

function submitSearch(event) {
  const value = event.target.value.trim()
  if (value) router.push({ path: '/search', query: { q: value } })
}

function onKeydown(event) {
  const tag = (document.activeElement && document.activeElement.tagName) || ''
  if (event.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(tag)) {
    event.preventDefault()
    searchInput.value?.focus()
  }
}

onMounted(() => window.addEventListener('keydown', onKeydown))
onUnmounted(() => window.removeEventListener('keydown', onKeydown))
</script>

<template>
  <header class="topbar">
    <button
      class="icon-btn sidebar-toggle"
      :title="ui.sidebarCollapsed ? '展开侧栏' : '收起侧栏'"
      @click="ui.toggleSidebar()"
    >
      <svg v-if="!ui.sidebarCollapsed" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/><path d="m15 9-3 3 3 3"/></svg>
      <svg v-else viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/><path d="m13 9 3 3-3 3"/></svg>
    </button>
    <div class="topbar-title">
      <h2>{{ title }}</h2>
    </div>
    <div class="global-search">
      <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
      <input
        id="global-search"
        ref="global-search"
        type="search"
        placeholder="搜索正文与章节…（/）"
        @keyup.enter="submitSearch"
      >
    </div>
    <div class="topbar-actions">
      <button class="icon-btn" title="切换主题" @click="ui.toggleTheme">
        <svg class="icon-sun" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
        <svg class="icon-moon" viewBox="0 0 24 24"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z"/></svg>
      </button>
    </div>
  </header>
</template>
