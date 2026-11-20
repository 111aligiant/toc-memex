<script setup>
/**
 * 面板：现有 .panel 的结构化版本（头 + 体），保留同样的视觉，
 * 但把 panel-head / panel-body 的间距收进组件，不再各处手写。
 */
const props = defineProps({
  title: { type: String, default: '' },
  sub: { type: String, default: '' },
  flush: { type: Boolean, default: false }, // 内容自己贴边（列表、表格）
})
</script>

<template>
  <section class="ui-panel">
    <header v-if="props.title || $slots.head || $slots.actions" class="ui-panel-head">
      <h3 v-if="props.title">{{ props.title }}</h3>
      <slot name="head" />
      <span v-if="props.sub" class="sub">{{ props.sub }}</span>
      <div v-if="$slots.actions" class="actions"><slot name="actions" /></div>
    </header>
    <div class="ui-panel-body" :class="{ flush: props.flush }">
      <slot />
    </div>
  </section>
</template>

<style scoped>
.ui-panel {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
}
.ui-panel-head {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 38px;
  padding: 8px 14px;
  border-bottom: 1px solid var(--border);
}
.ui-panel-head h3 { font-size: 12.5px; font-weight: 600; }
.ui-panel-head .sub { font-size: 11px; color: var(--faint); }
.ui-panel-head .actions { margin-left: auto; display: flex; align-items: center; gap: 6px; }
.ui-panel-body { padding: 12px 14px; }
.ui-panel-body.flush { padding: 0; }
</style>
