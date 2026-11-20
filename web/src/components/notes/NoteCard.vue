<script setup>
/**
 * 一条手记。作者徽标 / 类型 / 时间 / 正文 / 动作的唯一定义处。
 * 原先这套标记在 NotesView、NotesTab、SliceNotes 里各抄了一遍，
 * 改一处颜色要改三个文件——这里收敛成一处。
 */
import { KIND_LABELS } from '@/stores/notes'
import { relTime } from '@/utils/format'
import { authorColor, renderNoteBody } from '@/utils/notes'

const props = defineProps({
  note: { type: Object, required: true },
  compact: { type: Boolean, default: false },
  showDoc: { type: Boolean, default: false },
  replyable: { type: Boolean, default: true },
})

defineEmits(['reply', 'open-doc', 'open-chunk'])
</script>

<template>
  <article class="ui-note" :class="{ compact: props.compact }">
    <header class="head">
      <span class="author" :style="{ background: authorColor(note.author) }">{{ note.author }}</span>
      <span class="kind" :class="note.kind">{{ KIND_LABELS[note.kind] || note.kind }}</span>
      <button v-if="props.showDoc && note.docId" class="doc-link" @click="$emit('open-doc', note)">
        → 来源文档
      </button>
      <time class="time">{{ relTime(note.createdAt) }}</time>
    </header>
    <!-- eslint-disable-next-line vue/no-v-html -->
    <div class="body" v-html="renderNoteBody(note.body)"></div>
    <footer v-if="$slots.actions || props.replyable" class="actions">
      <button v-if="props.replyable" class="act" @click="$emit('reply', note)">回复</button>
      <slot name="actions" />
    </footer>
  </article>
</template>

<style scoped>
.ui-note { display: block; }
.head { display: flex; align-items: center; gap: 8px; }
.author {
  flex: none;
  border-radius: 999px;
  padding: 2px 8px;
  font-size: 10px;
  font-weight: 600;
  color: #fff;
  line-height: 1.5;
}
.kind { flex: none; font-size: 10px; color: var(--muted); }
.kind.pitfall { color: var(--danger); }
.kind.handoff { color: var(--accent); }
.kind.ingest { color: var(--ok); }
.time { margin-left: auto; flex: none; font-size: 10px; color: var(--faint); font-variant-numeric: tabular-nums; }
.body {
  margin-top: 6px;
  font-size: 12px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text);
}
.compact .body { margin-top: 5px; font-size: 11.5px; }

.actions { display: flex; gap: 8px; margin-top: 6px; }
.act,
.doc-link {
  border: 0;
  background: none;
  padding: 0;
  font-size: 11px;
  color: var(--muted);
  cursor: pointer;
  transition: color .12s var(--ease);
}
.act:hover,
.doc-link:hover { color: var(--accent); }
</style>
