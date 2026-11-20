<script setup>
/**
 * 分段开关。magpie 的做法：没有灰色轨道，只有“当前项”的滑块浮起来。
 * 用它替换现在散落各处的 .seg（工具条里的全部/笔记/踩坑/交接）。
 *
 * 语义用 radiogroup 而不是 tablist：它切换的是筛选条件，没有对应的 tabpanel。
 * 键盘按 radiogroup 的惯例走：Tab 进入一次，左右键在选项间移动。
 */
import { computed } from 'vue'

const props = defineProps({
  modelValue: { type: [String, Number], required: true },
  items: { type: Array, required: true }, // [{ value, label, count? }]
  size: { type: String, default: 'md' },
  ariaLabel: { type: String, default: '' },
})

const emit = defineEmits(['update:modelValue'])

const currentIndex = computed(() =>
  props.items.findIndex((item) => item.value === props.modelValue),
)

function move(step, event) {
  const count = props.items.length
  if (!count) return
  const from = currentIndex.value < 0 ? 0 : currentIndex.value
  const next = (from + step + count) % count
  emit('update:modelValue', props.items[next].value)
  event.currentTarget
    ?.querySelectorAll('[role="radio"]')
    ?.[next]?.focus()
}

function onKeydown(event) {
  if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
    event.preventDefault()
    move(1, event)
  } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
    event.preventDefault()
    move(-1, event)
  }
}
</script>

<template>
  <div
    class="ui-seg"
    :class="`s-${props.size}`"
    role="radiogroup"
    :aria-label="props.ariaLabel || undefined"
    @keydown="onKeydown"
  >
    <button
      v-for="item in props.items"
      :key="item.value"
      type="button"
      role="radio"
      :aria-checked="item.value === props.modelValue"
      :tabindex="item.value === props.modelValue ? 0 : -1"
      :class="{ on: item.value === props.modelValue }"
      @click="emit('update:modelValue', item.value)"
    >
      {{ item.label }}<span v-if="item.count" class="count">{{ item.count }}</span>
    </button>
  </div>
</template>

<style scoped>
.ui-seg {
  display: inline-flex;
  gap: 2px;
  padding: 2px;
  border-radius: 8px;
  background: var(--seg-track);
  border: 1px solid var(--pill-line);
}
.ui-seg button {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border: 0;
  background: none;
  border-radius: 6px;
  color: var(--muted);
  white-space: nowrap;
  cursor: pointer;
  transition: background .15s var(--ease), color .15s var(--ease), box-shadow .15s var(--ease);
}
.ui-seg.s-md button { height: 24px; padding: 0 11px; font-size: 12px; }
.ui-seg.s-sm button { height: 21px; padding: 0 8px; font-size: 11px; }
.ui-seg button:hover:not(.on) { color: var(--text); }
.ui-seg button.on {
  background: var(--seg-thumb);
  color: var(--text);
  font-weight: 500;
  box-shadow: var(--seg-thumb-edge);
}
.count { color: var(--faint); font-size: 10px; font-variant-numeric: tabular-nums; }
.on .count { color: var(--muted); }
</style>
