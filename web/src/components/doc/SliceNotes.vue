<script setup>
import { computed } from 'vue'
import NoteComposer from '@/components/notes/NoteComposer.vue'
import NoteCard from '@/components/notes/NoteCard.vue'
import { useNotesStore } from '@/stores/notes'
import { useWorkspaceStore } from '@/stores/workspace'
import UiButton from '@/components/ui/UiButton.vue'
import UiEmpty from '@/components/ui/UiEmpty.vue'

/**
 * Notes left on one slice: the list, plus a composer that writes here.
 * Shared by the 切片 and 正文 tabs so both write slice-level notes the same way.
 */
const props = defineProps({
  chunk: { type: Object, required: true },
  compact: { type: Boolean, default: false },
})

const emit = defineEmits(['open-notes'])

const workspace = useWorkspaceStore()
const notesStore = useNotesStore()

const notes = computed(() => notesStore.notesForChunk(props.chunk.id, props.chunk.nodeId))
</script>

<template>
  <div class="slice-notes" :class="{ compact }">
    <div v-for="note in notes" :key="note.id" class="slice-note">
      <NoteCard :note="note" compact :replyable="false" />
    </div>
    <UiEmpty
      v-if="!notes.length"
      compact
      title="这里还没有手记"
      hint="在下方写下第一条。"
    />
    <div v-if="notes.length" class="slice-notes-actions">
      <UiButton variant="quiet" size="sm" @click="emit('open-notes', { chunkId: chunk.id, nodeId: chunk.nodeId })">
        在手记 tab 查看 →
      </UiButton>
    </div>
    <NoteComposer
      :key="chunk.id"
      :anchor="{ docId: workspace.docId, nodeId: chunk.nodeId, chunkId: chunk.id }"
      :placeholder="compact ? '对这条切片写手记…' : '对这条切片写手记：问题、结论、修改建议…'"
    />
  </div>
</template>

<style scoped>
.slice-notes { margin-top: 4px; display: flex; flex-direction: column; gap: 8px; }
.slice-note {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 8px 10px;
  background: var(--surface-2);
}
.slice-notes-actions { display: flex; }
.slice-notes :deep(.note-composer textarea) { min-height: 56px; }
</style>
