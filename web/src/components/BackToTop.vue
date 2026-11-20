<script setup>
import { onMounted, onUnmounted, ref } from 'vue'

// The page scrolls the .content container, not the window.
const show = ref(false)
let scrollBox = null

function onScroll(event) {
  show.value = event.target.scrollTop > 320
}

function backToTop() {
  scrollBox?.scrollTo({ top: 0, behavior: 'smooth' })
}

onMounted(() => {
  scrollBox = document.querySelector('.content')
  scrollBox?.addEventListener('scroll', onScroll, { passive: true })
  onScroll({ target: scrollBox || { scrollTop: 0 } })
})

onUnmounted(() => {
  scrollBox?.removeEventListener('scroll', onScroll)
  scrollBox = null
})
</script>

<template>
  <button v-if="show" class="back-to-top" title="回到顶部" aria-label="回到顶部" @click="backToTop">
    <svg viewBox="0 0 24 24"><path d="M12 19V5M6 11l6-6 6 6"/></svg>
  </button>
</template>

<style scoped>
.back-to-top {
  position: fixed; right: 30px; bottom: 30px; z-index: 20;
  width: 34px; height: 34px;
  display: grid; place-items: center;
  border: 1px solid var(--border); border-radius: 50%;
  background: var(--surface); color: var(--muted);
  box-shadow: 0 2px 10px rgb(0 0 0 / 8%);
}
.back-to-top:hover { border-color: var(--border-strong); color: var(--accent); }
.back-to-top svg { width: 15px; height: 15px; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
</style>
