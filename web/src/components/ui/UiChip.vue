<script setup>
/**
 * 胶囊标签。用于状态、类型、计数、可点筛选。
 * tone 与全站语义色一一对应，不要传任意颜色。
 * 可点的时候必须同时给 as="button"，否则键盘到不了。
 */
import { computed } from 'vue'

const props = defineProps({
  tone: { type: String, default: '' }, // '' | accent | ok | warn | danger
  size: { type: String, default: 'md' }, // sm | md
  as: { type: String, default: 'span' }, // span | button
  active: { type: Boolean, default: false },
})

const clickable = computed(() => props.as === 'button')
</script>

<template>
  <component
    :is="props.as"
    class="ui-chip"
    :class="[`t-${props.tone || 'plain'}`, `s-${props.size}`, { clickable, active: props.active }]"
    :type="clickable ? 'button' : undefined"
  >
    <slot />
  </component>
</template>

<style scoped>
.ui-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border: 1px solid var(--pill-line);
  border-radius: 999px;
  background: var(--pill);
  color: var(--muted);
  font-weight: 500;
  white-space: nowrap;
  line-height: 1;
  transition: background .12s var(--ease), border-color .12s var(--ease), color .12s var(--ease);
}
.s-sm { height: 20px; padding: 0 8px; font-size: 10px; }
.s-md { height: 24px; padding: 0 10px; font-size: 11px; }

.t-accent { background: var(--accent-soft); border-color: color-mix(in srgb, var(--accent) 30%, var(--pill-line)); color: var(--accent); }
.t-ok { background: var(--ok-soft); border-color: color-mix(in srgb, var(--ok) 30%, var(--pill-line)); color: var(--ok); }
.t-warn { background: var(--warn-soft); border-color: color-mix(in srgb, var(--warn) 32%, var(--pill-line)); color: var(--warn); }
.t-danger { background: var(--danger-soft); border-color: color-mix(in srgb, var(--danger) 30%, var(--pill-line)); color: var(--danger); }

.clickable { cursor: pointer; }
.clickable:hover { border-color: color-mix(in srgb, var(--accent) 45%, var(--pill-line)); color: var(--accent); }
.active { background: var(--accent); border-color: var(--accent); color: #fff; }
[data-theme="dark"] .active { color: #0f1115; }
</style>
