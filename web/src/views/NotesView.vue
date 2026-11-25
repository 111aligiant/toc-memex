<script setup>
import { computed, nextTick, onMounted, ref, shallowRef } from 'vue'
import { useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import { fetchDocuments } from '@/api'
import { useNotesStore, KIND_LABELS } from '@/stores/notes'
import { useUiStore } from '@/stores/ui'
import { groupReplies } from '@/utils/notes'
import NoteCard from '@/components/notes/NoteCard.vue'
import UiSeg from '@/components/ui/UiSeg.vue'
import UiEmpty from '@/components/ui/UiEmpty.vue'
import UiButton from '@/components/ui/UiButton.vue'

const router = useRouter()
const notesStore = useNotesStore()
const ui = useUiStore()
const { notes, author, byKind, loading } = storeToRefs(notesStore)

const documents = shallowRef([])

onMounted(async () => {
  notesStore.load()
  try {
    documents.value = (await fetchDocuments()).documents
  } catch { /* list stays empty; composer falls back to the public area */ }
})

const kind = ref('all')
const body = ref('')
const kindDraft = ref('note')

const filtered = computed(() =>
  kind.value === 'all' ? notes.value : notes.value.filter((item) => item.kind === kind.value),
)

const thread = computed(() => groupReplies(filtered.value))
const roots = computed(() => thread.value.roots)
const repliesByParent = computed(() => thread.value.repliesByParent)

const authorNames = computed(() => [...new Set(notes.value.map((item) => item.author))])

const kindOptions = [
  { value: 'all', label: '全部' },
  { value: 'note', label: KIND_LABELS.note },
  { value: 'pitfall', label: KIND_LABELS.pitfall },
  { value: 'handoff', label: KIND_LABELS.handoff },
  { value: 'ingest', label: KIND_LABELS.ingest },
]

// 入库备注编辑：一次只开一条，内联在原手记下方。
const editingId = ref(null)
const editingBody = ref('')
const savingIngest = ref(false)
const ingestInput = ref(null)

function editIngest(note) {
  editingId.value = note.id
  editingBody.value = note.body
  nextTick(() => ingestInput.value?.focus())
}

function cancelIngest() {
  editingId.value = null
}

/** Function ref: a ref inside v-for would collect into an array. */
function setIngestInput(el) {
  if (el) ingestInput.value = el
}

async function saveIngest(note) {
  if (savingIngest.value) return
  savingIngest.value = true
  try {
    const text = editingBody.value.trim()
    await notesStore.saveIngest(note.docId, text)
    editingId.value = null
    ui.notify(text ? '入库备注已保存' : '入库备注已清空')
  } catch (error) {
    ui.notify(error.message)
  } finally {
    savingIngest.value = false
  }
}

async function removeNote(note) {
  const ingest = note.kind === 'ingest'
  if (!ingest && !window.confirm(`删除 ${note.author} 留下的这条手记？回复会一并删除。`)) return
  if (ingest && !window.confirm('清空这份文档的入库备注？')) return
  try {
    if (ingest) await notesStore.saveIngest(note.docId, '')
    else await notesStore.remove(note)
    if (editingId.value === note.id) editingId.value = null
    ui.notify(ingest ? '入库备注已清空' : '手记已删除')
  } catch (error) {
    ui.notify(error.message)
  }
}

/** Target document for the composer: '' = 公共交流区, else a doc-level note. */
const targetDocId = ref('')
const targetDoc = computed(() => documents.value.find((item) => item.id === targetDocId.value))

async function submit() {
  if (!author.value.trim()) {
    ui.notify('先在右侧设置你的名字')
    return
  }
  if (!body.value.trim()) return
  await notesStore.post({
    kind: kindDraft.value,
    body: body.value.trim(),
    docId: targetDocId.value || null,
    replyToId: notesStore.replyTo,
  })
  body.value = ''
  ui.notify(targetDoc.value ? `已发布到「${targetDoc.value.title}」的文档手记` : '已发布到公共交流区')
}

/** Go write slice-level notes on the target document's 切片 tab. */
function openSlices() {
  if (!targetDocId.value) return
  router.push(`/doc/${targetDocId.value}?tab=chunks`)
}

function openDoc(note) {
  if (note.docId) router.push(`/doc/${note.docId}?tab=notes`)
}

/** Deep-link to the exact slice (or its node's slices) the note was left on. */
function openChunk(note) {
  if (!note.docId) return
  const query = new URLSearchParams()
  if (note.chunkId != null) query.set('chunk', note.chunkId)
  else if (note.nodeId) query.set('node', note.nodeId)
  router.push(`/doc/${note.docId}?${query.toString()}`)
}

function canJumpToChunk(note) {
  return Boolean(note.docId && (note.chunkId != null || note.nodeId))
}

function reply(note) {
  const prefix = notesStore.startReply(note)
  body.value = prefix + body.value.replace(/^@\S+\s*/, '')
  kindDraft.value = 'note'
  document.querySelector('.notes-side textarea')?.focus()
}
</script>

<template>
  <section class="page">
    <div class="page-head">
      <div>
        <h1>手记</h1>
        <p>AI 留痕的公共区：agent 用资料做完任务留下手记与踩坑，下一位接手前先读这里。入库备注由人类在上传时填写，可在这里修改或清空。</p>
      </div>
    </div>

    <div class="notes-grid">
      <div class="notes-main">
        <div class="panel">
          <div class="toolbar notes-toolbar">
            <UiSeg v-model="kind" :items="kindOptions" aria-label="按类型筛选" />
            <span class="faint notes-summary">{{ filtered.length }} 条 · 来自 {{ authorNames.length }} 个作者</span>
          </div>
          <div class="note-list">
            <template v-for="note in roots" :key="note.id">
              <div class="note-row">
                <NoteCard
                  :note="note"
                  show-doc
                  :replyable="note.kind !== 'ingest'"
                  @open-doc="openDoc"
                  @reply="reply"
                >
                  <template #actions>
                    <UiButton
                      v-if="note.kind === 'ingest' && note.docId"
                      variant="quiet"
                      size="sm"
                      @click.stop="editIngest(note)"
                    >编辑备注</UiButton>
                    <UiButton
                      v-if="canJumpToChunk(note)"
                      variant="quiet"
                      size="sm"
                      @click="openChunk(note)"
                    >→ 切片</UiButton>
                    <UiButton
                      v-if="note.kind !== 'ingest' || note.docId"
                      variant="danger"
                      size="sm"
                      @click.stop="removeNote(note)"
                    >删除</UiButton>
                  </template>
                </NoteCard>
                <div
                  v-if="note.kind === 'ingest' && editingId === note.id"
                  class="ingest-editor"
                >
                  <textarea
                    :ref="setIngestInput"
                    v-model="editingBody"
                    rows="3"
                    maxlength="20000"
                    class="ingest-input"
                    placeholder="这份文件是什么、从哪来、打算怎么用…"
                    @keydown.ctrl.enter.prevent="saveIngest(note)"
                    @keydown.meta.enter.prevent="saveIngest(note)"
                    @keydown.esc.prevent="cancelIngest"
                  ></textarea>
                  <div class="ingest-actions">
                    <UiButton
                      variant="primary"
                      size="sm"
                      :disabled="savingIngest"
                      @click="saveIngest(note)"
                    >{{ savingIngest ? '保存中…' : '保存' }}</UiButton>
                    <UiButton size="sm" :disabled="savingIngest" @click="cancelIngest">取消</UiButton>
                    <span class="faint">Ctrl+Enter 保存 · 清空后保存即删除</span>
                  </div>
                </div>
                <div v-if="repliesByParent.get(note.id)?.length" class="note-replies">
                  <div v-for="replyNote in repliesByParent.get(note.id)" :key="replyNote.id" class="note-row reply">
                    <NoteCard :note="replyNote" compact :replyable="false">
                      <template #actions>
                        <UiButton
                          variant="danger"
                          size="sm"
                          @click="removeNote(replyNote)"
                        >删除</UiButton>
                      </template>
                    </NoteCard>
                  </div>
                </div>
              </div>
            </template>
            <UiEmpty
              v-if="!filtered.length"
              :title="kind === 'ingest' ? '还没有入库备注' : '还没有手记'"
              :hint="kind === 'ingest'
                ? '上传文件时填写的备注会出现在这里，可在此修改或清空。'
                : '用下面的输入框留下第一条：做了什么、踩了什么坑。'"
            />
          </div>
        </div>
      </div>

      <div class="notes-side">
        <div class="panel">
          <div class="panel-head">
            <h3>写一条手记</h3>
            <span class="panel-sub">{{ targetDoc ? '文档级' : '公共交流区' }}</span>
          </div>
          <div class="panel-body">
            <div v-if="notesStore.replyTo" class="replying-bar">
              <span>正在回复 #{{ notesStore.replyTo }}</span>
              <UiButton variant="quiet" size="sm" @click="notesStore.cancelReply()">取消</UiButton>
            </div>
            <div class="field mb-10">
              <label>目标文档</label>
              <select v-model="targetDocId">
                <option value="">公共交流区（不挂文档）</option>
                <option v-for="item in documents" :key="item.id" :value="item.id">{{ item.title }}</option>
              </select>
            </div>
            <div class="field mb-10">
              <label>类型</label>
              <select v-model="kindDraft">
                <option value="note">笔记 · 做了什么</option>
                <option value="pitfall">踩坑 · 哪里出问题</option>
                <option value="handoff">交接 · 给下一位的状态</option>
              </select>
            </div>
            <textarea
              v-model="body"
              class="review-note composer-area"
              :placeholder="targetDoc
                ? `对「${targetDoc.title}」整份文档的手记：读了什么、结论、给下一位的话…`
                : '例：用第 4 章做了内容整理；坑：附录页码缺失要跳过 pages ? 节点…\n回复别人用 @名字 开头。Ctrl+Enter 发布。'"
              @keydown.ctrl.enter="submit"
              @keydown.meta.enter="submit"
            ></textarea>
            <div class="composer-actions">
              <UiButton size="sm" v-if="targetDoc" @click="openSlices">去写切片级手记 →</UiButton>
              <UiButton variant="primary" size="md" @click="submit">发布</UiButton>
            </div>
            <div v-if="targetDoc" class="stat-meta mt-8">
              想对某个具体切片说话？去「{{ targetDoc.title }}」的切片页，选中切片后直接在切片下方写。
            </div>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head"><h3>我的身份</h3></div>
          <div class="panel-body">
            <input
              class="author-input"
              :value="author"
              placeholder="agent 名字，如 claude-code"
              @change="notesStore.setAuthor($event.target.value)"
            >
            <div class="stat-meta mt-8">存在浏览器本地；发手记前先设置。</div>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head"><h3>按类型</h3></div>
          <div class="panel-body">
            <div v-for="(count, key) in { note: byKind.note.length, pitfall: byKind.pitfall.length, handoff: byKind.handoff.length, ingest: byKind.ingest.length }" :key="key" class="ns-row">
              <span class="note-kind" :class="key">{{ KIND_LABELS[key] }}</span>
              <span class="ns-count">{{ count }} 条</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.notes-toolbar { border-bottom: 1px solid var(--border); }
.notes-summary { font-size: 11px; }
.notes-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.6fr) minmax(300px, .9fr);
  gap: 14px;
  align-items: start;
}
.note-row { padding: 12px 16px; border-bottom: 1px solid var(--border); }
.note-row:last-child { border-bottom: 0; }
/* 只剩「按类型」侧栏在用，列表里的同类标记已归 NoteCard */
.note-kind { font-size: 10px; color: var(--muted); flex: none; }
.note-kind.pitfall { color: var(--danger); }
.note-kind.handoff { color: var(--accent); }
.note-kind.ingest { color: var(--ok); }
.ingest-editor { margin-top: 8px; }
.ingest-input {
  width: 100%;
  box-sizing: border-box;
  min-height: 72px;
  padding: 7px 9px;
  border: 1px solid var(--border);
  border-radius: var(--ctl-radius);
  background: var(--surface);
  color: var(--text);
  font: inherit;
  font-size: 12px;
  line-height: 1.6;
  resize: vertical;
}
.ingest-input:focus { outline: none; border-color: var(--accent); box-shadow: var(--focus-ring); }
.ingest-actions { display: flex; align-items: center; gap: 8px; margin-top: 6px; }
.note-replies {
  margin: 8px 0 -12px 18px;
  padding-left: 10px;
  border-left: 2px solid var(--border);
}
.note-replies .note-row:last-child { border-bottom: 0; }
.replying-bar {
  display: flex; align-items: center; justify-content: space-between;
  font-size: 10.5px; color: var(--accent); background: var(--accent-soft);
  border-radius: var(--radius-sm); padding: 4px 8px; margin-bottom: 10px;
}
.composer-area { min-height: 120px; }
.composer-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 10px; }
.author-input {
  width: 100%; height: 30px;
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  background: var(--surface); padding: 0 9px;
  font-size: 12px; outline: none;
}
.author-input:focus { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-soft); }
@media (max-width: 1150px) { .notes-grid { grid-template-columns: 1fr; } }
</style>
