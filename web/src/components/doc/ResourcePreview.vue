<script setup>
import { computed } from 'vue'
import DOMPurify from 'dompurify'
import UiEmpty from '@/components/ui/UiEmpty.vue'
import UiButton from '@/components/ui/UiButton.vue'

const props = defineProps({
  resource: { type: Object, required: true },
})
const emit = defineEmits(['close'])

const html = computed(() =>
  props.resource.content
    ? DOMPurify.sanitize(props.resource.content, { USE_PROFILES: { html: true } })
    : '',
)
</script>

<template>
  <Teleport to="body">
    <div class="modal-backdrop" @click.self="emit('close')">
      <div class="modal resource-modal" :class="{ 'resource-modal--wide': resource.type === 'table' }">
        <div class="modal-head">
          <div>
            <h2>{{ resource.title || resource.id }}</h2>
            <p>{{ resource.pageLabel }} · {{ resource.type }}</p>
          </div>
          <button class="modal-close" @click="emit('close')">×</button>
        </div>
        <img
          v-if="resource.image"
          class="resource-preview-img"
          :src="resource.image"
          :alt="resource.title"
        >
        <div v-else-if="html" class="detail-body resource-preview-table" v-html="html"></div>
        <UiEmpty v-else compact title="暂无预览" hint="图片下载失败或资源缺失。" />
        <div class="modal-actions">
          <UiButton size="md" @click="emit('close')">关闭</UiButton>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.resource-modal--wide { width: min(1100px, 100%); }
.resource-preview-table {
  margin-top: 14px;
  max-height: min(70vh, 720px);
  overflow: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}
/* The table comes from OCR as v-html, so scoped styles need :deep(). */
.resource-preview-table :deep(table) {
  border-collapse: collapse;
  width: 100%;
  min-width: 420px;
  font-size: 12.5px;
  line-height: 1.55;
}
.resource-preview-table :deep(td),
.resource-preview-table :deep(th) {
  border: 1px solid var(--border);
  padding: 6px 9px;
  vertical-align: top;
  text-align: left;
  word-break: break-word;
}
.resource-preview-table :deep(tr:first-child td),
.resource-preview-table :deep(tr:first-child th) {
  background: var(--surface-2);
  font-weight: 600;
  position: sticky;
  top: 0;
  z-index: 1;
}
/* A merged cell is the group label; tint it so the span hierarchy reads. */
.resource-preview-table :deep(td[rowspan]),
.resource-preview-table :deep(td[colspan]) {
  background: color-mix(in srgb, var(--accent) 6%, var(--surface));
}
</style>
