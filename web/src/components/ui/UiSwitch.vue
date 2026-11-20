<script setup>
/**
 * 开关。与 UiSeg 同一套控件语言：轨道比滑块深一档，开态用强调色。
 * 语义用 role=switch：点一下立即生效，不等保存按钮。
 */
const props = defineProps({
  modelValue: { type: Boolean, default: false },
  disabled: { type: Boolean, default: false },
  ariaLabel: { type: String, default: '' },
})
const emit = defineEmits(['update:modelValue'])
</script>

<template>
  <button
    type="button"
    class="ui-switch"
    :class="{ on: props.modelValue }"
    role="switch"
    :aria-checked="props.modelValue"
    :aria-label="props.ariaLabel || undefined"
    :disabled="props.disabled"
    @click="emit('update:modelValue', !props.modelValue)"
  >
    <span class="thumb" />
  </button>
</template>

<style scoped>
.ui-switch {
  position: relative;
  width: 34px;
  height: 20px;
  flex: none;
  padding: 0;
  border: 1px solid var(--pill-line);
  border-radius: 10px;
  background: var(--seg-track);
  cursor: pointer;
  transition: background .15s var(--ease), border-color .15s var(--ease);
}
.ui-switch:hover:not(:disabled) { border-color: var(--border-strong); }
.ui-switch.on {
  background: var(--accent);
  border-color: var(--accent);
}
.ui-switch:disabled { opacity: .45; cursor: not-allowed; }
.thumb {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: var(--seg-thumb);
  box-shadow: var(--seg-thumb-edge);
  transition: left .15s var(--ease);
}
.ui-switch.on .thumb { left: 16px; }
</style>