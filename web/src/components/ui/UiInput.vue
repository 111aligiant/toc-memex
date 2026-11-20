<script setup>
/**
 * 文本输入 / 下拉 / 多行文本的统一外壳。
 * 与 magpie 一致：1px 细边 + 圆角 7 + focus 时柔光环。
 *
 * inheritAttrs 关掉：原生属性（id / name / autofocus / aria-* / data-*）
 * 要落在真正的控件上，而不是外面那层 label。否则 getElementById、
 * label 关联、表单提交全部会失效。
 */
import { useAttrs } from 'vue'

defineOptions({ inheritAttrs: false })

const props = defineProps({
  modelValue: { type: [String, Number], default: '' },
  type: { type: String, default: 'text' }, // text | search | password | number | email | textarea | select
  placeholder: { type: String, default: '' },
  size: { type: String, default: 'md' }, // sm | md | lg
  rows: { type: Number, default: 3 },
  disabled: { type: Boolean, default: false },
  icon: { type: Boolean, default: false }, // 左侧放大镜
})

const emit = defineEmits(['update:modelValue', 'change', 'keyup', 'enter'])
const attrs = useAttrs()

function onInput(event) {
  emit('update:modelValue', event.target.value)
}
function onChange(event) {
  emit('update:modelValue', event.target.value)
  emit('change', event)
}
function onKeyup(event) {
  if (event.key === 'Enter') emit('enter', event)
  emit('keyup', event)
}
</script>

<template>
  <label class="ui-input-wrap" :class="[`s-${props.size}`, { 'is-disabled': props.disabled }]">
    <svg v-if="props.icon" class="lead" viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>
    </svg>
    <textarea
      v-if="props.type === 'textarea'"
      v-bind="attrs"
      class="ui-input"
      :value="props.modelValue"
      :placeholder="props.placeholder"
      :rows="props.rows"
      :disabled="props.disabled"
      @input="onInput"
      @change="onChange"
      @keyup="onKeyup"
    ></textarea>
    <select
      v-else-if="props.type === 'select'"
      v-bind="attrs"
      class="ui-input"
      :value="props.modelValue"
      :disabled="props.disabled"
      @change="onChange"
    >
      <slot />
    </select>
    <input
      v-else
      v-bind="attrs"
      class="ui-input"
      :type="props.type"
      :value="props.modelValue"
      :placeholder="props.placeholder"
      :disabled="props.disabled"
      @input="onInput"
      @change="onChange"
      @keyup="onKeyup"
    >
  </label>
</template>

<style scoped>
.ui-input-wrap {
  position: relative;
  display: flex;
  align-items: center;
  min-width: 0;
}
.ui-input {
  width: 100%;
  min-width: 0;
  border: 1px solid var(--border);
  border-radius: var(--ctl-radius);
  background: var(--surface);
  color: var(--text);
  outline: none;
  transition: border-color .12s var(--ease), box-shadow .12s var(--ease);
}
.ui-input::placeholder { color: var(--faint); }
.ui-input:hover:not(:disabled) { border-color: var(--border-strong); }
.ui-input:focus { border-color: var(--accent); box-shadow: var(--focus-ring); }
.ui-input:disabled { opacity: .5; cursor: not-allowed; }

.s-sm .ui-input { height: var(--ctl-h-sm); padding: 0 8px; font-size: 11px; }
.s-md .ui-input { height: var(--ctl-h); padding: 0 10px; font-size: 12px; }
.s-lg .ui-input { height: var(--ctl-h-lg); padding: 0 12px; font-size: 12.5px; }

/* textarea 不固定高度 */
.s-sm .ui-input:is(textarea),
.s-md .ui-input:is(textarea),
.s-lg .ui-input:is(textarea) {
  height: auto;
  padding: 7px 9px;
  line-height: 1.55;
  resize: vertical;
}

.lead {
  position: absolute;
  left: 9px;
  width: 14px;
  height: 14px;
  fill: none;
  stroke: var(--faint);
  stroke-width: 1.9;
  stroke-linecap: round;
  pointer-events: none;
}
.ui-input-wrap:has(.lead) .ui-input { padding-left: 28px; }
.is-disabled .lead { opacity: .5; }

/* select 用自绘箭头，右侧留位 */
select.ui-input { padding-right: 26px; appearance: none; cursor: pointer; }
.ui-input-wrap:has(select)::after {
  content: "";
  position: absolute;
  right: 10px;
  width: 6px;
  height: 6px;
  border-right: 1.5px solid var(--faint);
  border-bottom: 1.5px solid var(--faint);
  transform: translateY(-2px) rotate(45deg);
  pointer-events: none;
}
.is-disabled::after { opacity: .5; }
</style>
