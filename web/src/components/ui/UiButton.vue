<script setup>
/**
 * 按钮。三种视觉层级，与 magpie 一致：
 *   primary — 页面上唯一的主行动（发布、保存）
 *   quiet   — 默认；静默底 + 细边，读作“弹出按钮”，不是描边输入框
 *   ghost   — 无底无边，用于工具栏与行内文字动作
 * size: sm(24) / md(28) / lg(32)，全站只有这三档。
 */
const props = defineProps({
  variant: { type: String, default: 'quiet' }, // primary | quiet | ghost | danger
  size: { type: String, default: 'md' }, // sm | md | lg
  disabled: { type: Boolean, default: false },
  block: { type: Boolean, default: false },
  type: { type: String, default: 'button' },
})
</script>

<template>
  <button
    :type="props.type"
    class="ui-btn"
    :class="[`v-${props.variant}`, `s-${props.size}`, { block: props.block }]"
    :disabled="props.disabled"
  >
    <slot />
  </button>
</template>

<style scoped>
.ui-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  border: 1px solid transparent;
  border-radius: var(--ctl-radius);
  padding: 0 11px;
  font-size: 12px;
  font-weight: 500;
  line-height: 1;
  white-space: nowrap;
  cursor: pointer;
  transition: background .12s var(--ease), border-color .12s var(--ease),
              color .12s var(--ease), box-shadow .12s var(--ease);
}
.ui-btn:disabled { opacity: .45; cursor: not-allowed; }
.ui-btn.block { display: flex; width: 100%; }

/* 三档高度 */
.s-sm { height: var(--ctl-h-sm); padding: 0 8px; font-size: 11px; }
.s-md { height: var(--ctl-h); }
.s-lg { height: var(--ctl-h-lg); padding: 0 14px; font-size: 12.5px; }

/* quiet：静默底，细边，hover 时边框轻微染色 */
.v-quiet {
  background: var(--pill);
  border-color: var(--pill-line);
  color: var(--muted);
}
.v-quiet:hover:not(:disabled) {
  background: var(--pill-hover);
  border-color: var(--border-strong);
  color: var(--text);
}

/* primary：全站唯一强调色按钮 */
.v-primary {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
}
.v-primary:hover:not(:disabled) {
  background: color-mix(in srgb, var(--accent) 88%, #000);
  border-color: color-mix(in srgb, var(--accent) 88%, #000);
}
[data-theme="dark"] .v-primary { color: #0f1115; }
[data-theme="dark"] .v-primary:hover:not(:disabled) {
  background: color-mix(in srgb, var(--accent) 88%, #fff);
  border-color: color-mix(in srgb, var(--accent) 88%, #fff);
}

/* ghost：无底无边，hover 才出现一层静默底 */
.v-ghost {
  background: transparent;
  border-color: transparent;
  color: var(--muted);
}
.v-ghost:hover:not(:disabled) {
  background: var(--pill);
  color: var(--text);
}

/* danger：破坏性动作，平时静默，hover 才亮红 */
.v-danger {
  background: transparent;
  border-color: var(--border);
  color: var(--muted);
}
.v-danger:hover:not(:disabled) {
  background: var(--danger-soft);
  border-color: color-mix(in srgb, var(--danger) 45%, var(--border));
  color: var(--danger);
}
</style>
