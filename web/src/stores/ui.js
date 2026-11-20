import { computed, ref, watch } from 'vue'
import { defineStore } from 'pinia'

const THEME_KEY = 'kb-theme'
const SIDEBAR_KEY = 'kb-sidebar-collapsed'

function readStored(key, fallback) {
  try {
    const value = localStorage.getItem(key)
    return value === null ? fallback : value
  } catch {
    return fallback
  }
}

function writeStored(key, value) {
  try {
    localStorage.setItem(key, value)
  } catch { /* private mode */ }
}

export const useUiStore = defineStore('ui', () => {
  const theme = ref(readStored(THEME_KEY, 'light'))
  const sidebarCollapsed = ref(readStored(SIDEBAR_KEY, '0') === '1')
  const toast = ref('')

  watch(theme, (value) => {
    document.documentElement.dataset.theme = value
    writeStored(THEME_KEY, value)
  }, { immediate: true })

  watch(sidebarCollapsed, (value) => {
    writeStored(SIDEBAR_KEY, value ? '1' : '0')
  })

  function toggleTheme() {
    theme.value = theme.value === 'light' ? 'dark' : 'light'
  }

  function toggleSidebar() {
    sidebarCollapsed.value = !sidebarCollapsed.value
  }

  let toastTimer = null
  function notify(message) {
    toast.value = message
    clearTimeout(toastTimer)
    toastTimer = setTimeout(() => {
      toast.value = ''
    }, 2400)
  }

  return {
    theme,
    sidebarCollapsed,
    toast,
    toggleTheme,
    toggleSidebar,
    notify,
  }
})
