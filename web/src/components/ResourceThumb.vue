<script setup>
import { computed } from 'vue'

const props = defineProps({
  resource: { type: Object, required: true },
  compact: { type: Boolean, default: false },
})

const emit = defineEmits(['open'])

const placeholder = computed(() => {
  const kind = props.resource.type === 'table' ? 'table' : 'figure'
  const color = props.resource.type === 'table' ? '#0d9488' : '#8b5cf6'
  const label = (props.resource.title || props.resource.id).replace(/&/g, '&amp;')
  const art = kind === 'table'
    ? `<g stroke="#d5dbe5" stroke-width="1"><rect x="38" y="44" width="284" height="112" fill="#fff"/><path d="M38 72H322M38 100H322M38 128H322M110 44V156M190 44V156M258 44V156"/></g><g fill="${color}" opacity=".8"><rect x="48" y="82" width="48" height="6" rx="3"/><rect x="48" y="110" width="40" height="6" rx="3"/><rect x="120" y="82" width="55" height="6" rx="3"/><rect x="120" y="110" width="46" height="6" rx="3"/></g>`
    : `<g fill="none" stroke="${color}" stroke-width="2"><rect x="42" y="84" width="74" height="42" rx="5"/><rect x="154" y="48" width="74" height="42" rx="5"/><rect x="154" y="120" width="74" height="42" rx="5"/><path d="M116 105h38M228 69l26 26M228 141l26-26"/></g>`
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="360" height="200" viewBox="0 0 360 200"><rect width="360" height="200" fill="#f8fafc"/><rect x="0.5" y="0.5" width="359" height="199" fill="none" stroke="#e5e7eb"/>${art}<text x="18" y="190" font-family="system-ui, sans-serif" font-size="11" fill="#9aa1ac">${label}</text></svg>`
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`
})

const src = computed(() => props.resource.image || placeholder.value)
</script>

<template>
  <button class="chunk-block-resource" @click.stop="emit('open', resource)">
    <img :src="src" :alt="resource.title || resource.id" loading="lazy">
    <span>{{ resource.title || resource.id }}</span>
  </button>
</template>
