<script setup>
/**
 * 一条手记。作者徽标 / 类型 / 时间 / 正文 / 动作的唯一定义处。
 * 原先这套标记在 NotesView、NotesTab、SliceNotes 里各抄了一遍，
 * 改一处颜色要改三个文件——这里收敛成一处。
 */
import { ref } from 'vue'
import { KIND_LABELS } from '@/stores/notes'
import { fetchNoteHistory } from '@/api'
import { relTime } from '@/utils/format'
import { authorColor, renderNoteBody } from '@/utils/notes'

const props = defineProps({
  note: { type: Object, required: true },
  compact: { type: Boolean, default: false },
  showDoc: { type: Boolean, default: false },
  replyable: { type: Boolean, default: true },
})

defineEmits(['reply', 'open-doc', 'open-chunk'])

const history = ref(null)
const historyOpen = ref(false)
const historyError = ref('')

async function toggleHistory() {
  if (historyOpen.value) {
    historyOpen.value = false
    return
  }
  historyOpen.value = true
  if (history.value || !props.note.currentRev || props.note.currentRev < 2) return
  try {
    history.value = (await fetchNoteHistory(props.note.id)).revisions
  } catch (error) {
    historyError.value = error?.message || '读取修订历史失败'
  }
}
</script>

<template>
  <article class="ui-note" :class="{ compact: props.compact }">
    <header class="head">
      <span class="author" :style="{ background: authorColor(note.author) }">{{ note.author }}</span>
      <span class="kind" :class="note.kind">{{ KIND_LABELS[note.kind] || note.kind }}</span>
      <button v-if="props.showDoc && note.docId" class="doc-link" @click="$emit('open-doc', note)">
        → 来源文档
      </button>
      <button
        v-if="(note.currentRev || 1) > 1"
        class="rev"
        :title="note.revisedBy ? `最后由 ${note.revisedBy} 修订` : '已修订'"
        @click="toggleHistory"
      >
        第 {{ note.currentRev }} 版
      </button>
      <span v-if="note.retractedAt" class="retracted">已撤回</span>
      <time class="time">{{ relTime(note.createdAt) }}</time>
    </header>
    <!-- eslint-disable-next-line vue/no-v-html -->
    <div class="body" v-html="renderNoteBody(note.body)"></div>
    <section v-if="historyOpen && (note.currentRev || 1) > 1" class="history">
      <p v-if="historyError" class="history-error">{{ historyError }}</p>
      <ol v-else-if="history" class="revisions">
        <li v-for="item in history" :key="item.rev" class="revision">
          <header class="rev-head">
            <span class="rev-num">第 {{ item.rev }} 版</span>
            <span class="rev-author">{{ item.author }}</span>
            <span v-if="item.reason" class="rev-reason">{{ item.reason }}</span>
          </header>
          <p class="rev-body">{{ item.body }}</p>
        </li>
      </ol>
      <p v-else class="history-loading">读取中…</p>
    </section>
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
.rev,
.retracted {
  flex: none;
  border: 0;
  background: none;
  padding: 0;
  font-size: 10px;
  color: var(--muted);
  cursor: pointer;
}
.rev:hover { color: var(--accent); }
.retracted { color: var(--danger); cursor: default; }
.history { margin-top: 8px; border-top: 1px solid var(--line); padding-top: 6px; }
.revisions { list-style: none; margin: 0; padding: 0; display: grid; gap: 8px; }
.revision { display: grid; gap: 3px; }
.rev-head { display: flex; gap: 8px; align-items: baseline; font-size: 10px; color: var(--muted); }
.rev-num { color: var(--faint); font-variant-numeric: tabular-nums; }
.rev-author { font-weight: 600; }
.rev-reason::before { content: '· '; color: var(--faint); }
.rev-body {
  margin: 0;
  font-size: 11.5px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--muted);
}
.history-loading,
.history-error { margin: 0; font-size: 11px; color: var(--faint); }
.history-error { color: var(--danger); }
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
