<script setup>
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import AppSidebar from '@/components/AppSidebar.vue'
import AppTopbar from '@/components/AppTopbar.vue'
import { useUiStore } from '@/stores/ui'

const route = useRoute()
const ui = useUiStore()
const contentEl = ref(null)

// The scroll container outlives every route, so a real navigation must reset
// it; otherwise the next page opens halfway down. Run before the new view
// mounts so a deep link (e.g. ?chunk=) can still scroll to its target after.
// Query-only changes (a tab switch) keep their position.
watch(() => route.path, () => {
  if (contentEl.value) contentEl.value.scrollTop = 0
}, { flush: 'pre' })

const topbarTitle = computed(() => {
  if (route.path.startsWith('/doc/')) return '文档'
  return route.meta.title || 'Toc Memex'
})
</script>

<template>
  <AppSidebar />
  <div class="shell">
    <AppTopbar :title="topbarTitle" />
    <main ref="contentEl" class="content">
      <RouterView />
    </main>
  </div>
  <div class="toast" :class="{ show: ui.toast }">{{ ui.toast }}</div>
</template>
