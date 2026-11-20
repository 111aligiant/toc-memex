<script setup>
/**
 * 空状态。现有代码里每处都是手写 <div class="empty"><b>…</b>…</div>，
 * 文案层级不一致。这个组件固定“一句结论 + 一句引导”。
 */
const props = defineProps({
  title: { type: String, required: true },
  hint: { type: String, default: '' },
  compact: { type: Boolean, default: false },
})
</script>

<template>
  <div class="ui-empty" :class="{ compact: props.compact }">
    <div class="title">{{ props.title }}</div>
    <div v-if="props.hint" class="hint">{{ props.hint }}</div>
    <slot />
  </div>
</template>

<style scoped>
.ui-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  padding: 40px 20px;
  text-align: center;
}
.ui-empty.compact { padding: 20px 12px; }
.title { font-size: 12.5px; font-weight: 600; color: var(--text); }
.hint {
  font-size: 11.5px;
  color: var(--faint);
  line-height: 1.6;
  max-width: 42ch;
  text-wrap: balance;
}
</style>
