<script setup>
/**
 * Pick a collection for the selected documents. Clicking a row files them
 * immediately — a two-step confirm here would just be one more click for
 * an action that is trivially reversible (pick another row).
 */
import { useCollectionsStore } from '@/stores/collections'
import UiButton from '@/components/ui/UiButton.vue'

const props = defineProps({
  open: { type: Boolean, default: false },
  count: { type: Number, default: 0 },
})
const emit = defineEmits(['close', 'assign'])

const collections = useCollectionsStore()
</script>

<template>
  <Teleport to="body">
    <div v-if="props.open" class="modal-backdrop" @click.self="emit('close')">
      <div class="modal picker-modal">
        <div class="modal-head">
          <div>
            <h2>移入分区</h2>
            <p>已选 {{ props.count }} 份文档</p>
          </div>
          <button class="modal-close" @click="emit('close')">×</button>
        </div>

        <div class="picker-list">
          <button class="picker-row" @click="emit('assign', null)">
            <span class="picker-name">未分区</span>
            <span class="picker-count">{{ collections.unassigned }} 份</span>
          </button>
          <button
            v-for="item in collections.items"
            :key="item.id"
            class="picker-row"
            @click="emit('assign', item.id)"
          >
            <span class="picker-name">{{ item.name }}</span>
            <span class="picker-count">{{ item.docCount }} 份</span>
          </button>
        </div>

        <div class="modal-actions">
          <UiButton size="md" @click="emit('close')">取消</UiButton>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.picker-modal { width: min(420px, 100%); }
.picker-list { margin-top: 14px; display: flex; flex-direction: column; gap: 2px; }
.picker-row {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 9px 10px;
  border: 1px solid transparent;
  border-radius: var(--ctl-radius);
  background: none;
  color: var(--text);
  font-size: 12px;
  text-align: left;
  cursor: pointer;
}
.picker-row:hover { background: var(--pill-hover); }
.picker-name { flex: 1; }
.picker-count { font-size: 10.5px; color: var(--faint); }
</style>
