<script setup>
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import NoteComposer from '@/components/notes/NoteComposer.vue'
import NoteCard from '@/components/notes/NoteCard.vue'
import UiSeg from '@/components/ui/UiSeg.vue'
import { KIND_LABELS, useNotesStore } from '@/stores/notes'
import { useWorkspaceStore } from '@/stores/workspace'
import { authorColor, groupReplies } from '@/utils/notes'
import UiEmpty from '@/components/ui/UiEmpty.vue'
import UiButton from '@/components/ui/UiButton.vue'

const workspace = useWorkspaceStore()
const notesStore = useNotesStore()
const { docNotes, anchorFocus } = storeToRefs(notesStore)

const kind = ref('all')
// Plain object on purpose: writing to a reactive ref inside a template ref
// callback would re-trigger the render that created it (infinite loop).
const groupEls = {}

const filtered = computed(() =>
  kind.value === 'all' ? docNotes.value : docNotes.value.filter((note) => note.kind === kind.value),
)

const kinds = computed(() => [
  { value: 'all', label: '全部', count: docNotes.value.length },
  { value: 'note', label: KIND_LABELS.note, count: docNotes.value.filter((n) => n.kind === 'note').length },
  { value: 'pitfall', label: KIND_LABELS.pitfall, count: docNotes.value.filter((n) => n.kind === 'pitfall').length },
  { value: 'handoff', label: KIND_LABELS.handoff, count: docNotes.value.filter((n) => n.kind === 'handoff').length },
])

const authorNames = computed(() => [...new Set(docNotes.value.map((note) => note.author))])

function anchorKey(anchor) {
  if (!anchor) return 'doc'
  if (anchor.chunkId != null) return `chunk:${anchor.chunkId}`
  if (anchor.nodeId) return `node:${anchor.nodeId}`
  return 'doc'
}

/** Notes grouped by the place they were left on: chunk → node → whole doc. */
const groups = computed(() => {
  const chunkById = new Map(workspace.chunks.map((chunk) => [chunk.id, chunk]))
  const nodeById = new Map(workspace.nodes.map((node) => [node.node_id, node]))
  const buckets = new Map()

  for (const note of filtered.value) {
    const key = anchorKey({ chunkId: note.chunkId, nodeId: note.nodeId })
    if (!buckets.has(key)) buckets.set(key, { key, order: 0, label: '', sub: '', anchor: {}, notes: [] })
    const group = buckets.get(key)
    group.notes.push(note)

    if (key === 'doc') {
      group.label = '整份文档'
      group.sub = '文档级'
      group.order = -1
    } else if (key.startsWith('chunk:')) {
      const chunk = chunkById.get(note.chunkId)
      group.label = chunk ? `#${chunk.index} ${chunk.title || '(无标题)'}` : `切片 #${note.chunkId}`
      group.sub = chunk ? chunk.pages : ''
      group.order = chunk ? chunk.index : 1e6
      group.anchor = { chunkId: note.chunkId, nodeId: chunk?.nodeId ?? null }
    } else {
      const node = nodeById.get(note.nodeId)
      group.label = node ? node.title : note.nodeId
      group.sub = node ? node.pageLabel : '目录节点'
      group.order = node ? (workspace.nodes.indexOf(node) + 0.5) : 1e6
      group.anchor = { nodeId: note.nodeId }
    }
  }

  return [...buckets.values()]
    .sort((a, b) => a.order - b.order)
    .map((group) => ({ ...group, thread: groupReplies(group.notes) }))
})

const focusedKey = computed(() => (anchorFocus.value ? anchorKey(anchorFocus.value) : null))

const composeAnchor = computed(() => {
  const anchor = anchorFocus.value
  if (!anchor) return { docId: workspace.docId }
  if (anchor.chunkId != null) {
    const chunk = workspace.chunks.find((item) => item.id === anchor.chunkId)
    return { docId: workspace.docId, nodeId: chunk?.nodeId ?? null, chunkId: anchor.chunkId }
  }
  if (anchor.nodeId) return { docId: workspace.docId, nodeId: anchor.nodeId }
  return { docId: workspace.docId }
})

const composeLabel = computed(() => {
  const anchor = composeAnchor.value
  if (anchor.chunkId != null) {
    const chunk = workspace.chunks.find((item) => item.id === anchor.chunkId)
    return chunk ? `#${chunk.index} ${chunk.title || '(无标题)'}` : '当前切片'
  }
  if (anchor.nodeId) {
    const node = workspace.nodes.find((item) => item.node_id === anchor.nodeId)
    return node ? node.title : '当前目录节点'
  }
  return '整份文档'
})

function setGroupEl(key, el) {
  if (el) groupEls[key] = el
  else delete groupEls[key]
}

async function scrollToFocused() {
  const key = focusedKey.value
  if (!key) return
  await nextTick()
  groupEls[key]?.scrollIntoView({ block: 'start', behavior: 'smooth' })
}

watch(focusedKey, scrollToFocused)
watch(kind, scrollToFocused)
onMounted(scrollToFocused)

function openInRead(anchor) {
  if (anchor.chunkId != null) {
    workspace.selectedChunkId = anchor.chunkId
  } else if (anchor.nodeId) {
    const node = workspace.nodes.find((item) => item.node_id === anchor.nodeId)
    if (node) workspace.selectNode(node)
  }
  workspace.tab = 'read'
  if (workspace.selectedChunkId != null) workspace.scrollTarget = workspace.selectedChunkId
}

/** Jump straight to the slice this note is anchored to (切片 tab). */
function openInChunks(note) {
  workspace.openChunk({ chunkId: note.chunkId ?? null, nodeId: note.nodeId ?? null })
}

function canJumpToChunk(note) {
  if (note.chunkId != null) return true
  if (!note.nodeId) return false
  return workspace.chunks.some((chunk) => chunk.nodeId === note.nodeId)
}

function reply(note) {
  notesStore.startReply(note)
  notesStore.focusAnchor({
    chunkId: note.chunkId ?? null,
    nodeId: note.nodeId ?? null,
  })
  nextTick(() => document.querySelector('.notes-tab .note-composer textarea')?.focus())
}

function focusGroup(anchor) {
  notesStore.focusAnchor(anchor)
}
</script>

<template>
  <div class="notes-tab">
    <section class="panel notes-main">
      <div class="toolbar notes-toolbar">
        <UiSeg v-model="kind" :items="kinds" aria-label="按类型筛选" />
        <span class="faint notes-summary">{{ docNotes.length }} 条 · 来自 {{ authorNames.length }} 个 agent</span>
        <UiButton variant="quiet" size="sm" v-if="focusedKey && focusedKey !== 'doc'" @click="focusGroup({})">看全部</UiButton>
      </div>

      <div class="note-groups">
        <section
          v-for="group in groups"
          :key="group.key"
          :ref="el => setGroupEl(group.key, el)"
          class="note-group"
          :class="{ focused: group.key === focusedKey }"
        >
          <div class="note-group-head">
            <span class="note-group-label">{{ group.label }}</span>
            <span v-if="group.sub" class="note-group-sub">{{ group.sub }}</span>
            <span class="faint note-group-count">{{ group.notes.length }}</span>
            <UiButton variant="quiet" size="sm" v-if="group.key !== 'doc'"
              
              @click="openInRead(group.anchor)"
            >→ 正文</UiButton>
          </div>

          <div v-for="note in group.thread.roots" :key="note.id" class="note-row">
            <NoteCard :note="note" @reply="reply">
              <template #actions>
                <UiButton
                  v-if="canJumpToChunk(note)"
                  variant="quiet"
                  size="sm"
                  @click="openInChunks(note)"
                >→ 切片</UiButton>
              </template>
            </NoteCard>
            <div v-if="group.thread.repliesByParent.get(note.id)?.length" class="note-replies">
              <div v-for="replyNote in group.thread.repliesByParent.get(note.id)" :key="replyNote.id" class="note-row reply">
                <NoteCard :note="replyNote" compact :replyable="false" />
              </div>
            </div>
          </div>
        </section>

        <UiEmpty
          v-if="!groups.length"
          :title="kind === 'all' ? '这份文档还没有手记' : `没有${KIND_LABELS[kind]}类手记`"
          :hint="kind === 'all' ? '在右侧写下第一条：做了什么、踩了什么坑，下一位接手前会看到。' : '换个类型看看，或在右侧写一条。'"
        />
      </div>
    </section>

    <aside class="notes-side">
      <div class="panel">
        <div class="panel-head">
          <h3>写一条手记</h3>
          <span class="panel-sub">{{ composeLabel }}</span>
        </div>
        <div class="panel-body">
          <div v-if="composeAnchor.chunkId != null || composeAnchor.nodeId" class="anchor-bar">
            <span>锚定：{{ composeLabel }}</span>
            <UiButton variant="quiet" size="sm" @click="focusGroup({})">改到整份文档</UiButton>
          </div>
          <NoteComposer
            :anchor="composeAnchor"
            placeholder="例：用第4章做了温度剖面整理；坑：附录页码缺失要跳过 pages ? 节点…"
            @posted="focusGroup(composeAnchor)"
          />
        </div>
      </div>

      <div class="panel">
        <div class="panel-head"><h3>我的身份</h3></div>
        <div class="panel-body">
          <input
            class="author-input"
            :value="notesStore.author"
            placeholder="agent 名字，如 claude-code"
            @change="notesStore.setAuthor($event.target.value)"
          >
          <div class="stat-meta mt-8">存在浏览器本地；写手记前先设置。</div>
        </div>
      </div>

      <div class="panel">
        <div class="panel-head"><h3>按 agent</h3></div>
        <div class="panel-body">
          <div v-for="name in authorNames" :key="name" class="ns-row">
            <span class="note-author" :style="{ background: authorColor(name) }">{{ name }}</span>
            <span class="ns-count">{{ docNotes.filter(note => note.author === name).length }} 条</span>
          </div>
          <div v-if="!authorNames.length" class="stat-meta">还没有人在这份文档上留痕。</div>
        </div>
      </div>
    </aside>
  </div>
</template>

<style scoped>
.notes-tab {
  display: grid;
  grid-template-columns: minmax(0, 1.7fr) minmax(300px, .9fr);
  gap: 14px;
  align-items: start;
  margin-top: 14px;
}
.notes-toolbar { border-bottom: 1px solid var(--border); }
.notes-summary { font-size: 11px; }
.note-group { border-bottom: 1px solid var(--border); scroll-margin-top: 12px; }
.note-group:last-child { border-bottom: 0; }
.note-group.focused { background: var(--accent-soft); }
.note-group-head {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 16px;
  background: var(--surface-2);
  border-bottom: 1px solid var(--border);
  font-size: 11px;
}
.note-group-label { font-weight: 500; }
.note-group-sub { font-size: 10px; color: var(--faint); }
.note-group-count { margin-left: auto; font-size: 10px; }
.note-row { padding: 11px 16px; border-bottom: 1px solid var(--border); }
.note-row:last-child { border-bottom: 0; }
/* 「按 agent」侧栏仍在用 */
.note-author {
  color: #fff; font-size: 10px; font-weight: 600;
  border-radius: 999px; padding: 2px 8px; flex: none;
}
.note-replies { margin: 8px 0 -11px 16px; border-left: 2px solid var(--border); }
.note-replies .note-row:last-child { border-bottom: 0; }
.anchor-bar {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  font-size: 10.5px; color: var(--accent); background: var(--accent-soft);
  border-radius: var(--radius-sm); padding: 4px 8px; margin-bottom: 8px;
}
.ns-row .ns-count { margin-left: auto; }
.author-input {
  width: 100%; height: 30px;
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  background: var(--surface); padding: 0 9px;
  font-size: 12px; outline: none;
}
.author-input:focus { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-soft); }
@media (max-width: 1150px) {
  .notes-tab { grid-template-columns: 1fr; }
}
</style>
