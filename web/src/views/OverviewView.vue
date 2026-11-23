<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import { useLibraryStore } from '@/stores/library'
import { useTasksStore } from '@/stores/quality'
import { useNotesStore, KIND_LABELS } from '@/stores/notes'
import { useUiStore } from '@/stores/ui'
import { fetchDocumentChunks } from '@/api'
import { relTime } from '@/utils/format'
import { authorColor } from '@/utils/notes'
import NoteCard from '@/components/notes/NoteCard.vue'
import UiSeg from '@/components/ui/UiSeg.vue'
import UiEmpty from '@/components/ui/UiEmpty.vue'
import UiButton from '@/components/ui/UiButton.vue'

const router = useRouter()
const library = useLibraryStore()
const tasksStore = useTasksStore()
const notesStore = useNotesStore()
const ui = useUiStore()
const { notes } = storeToRefs(notesStore)

/* ── panel collapse, remembered per browser ─────────────────────── */
const PANEL_KEY = 'tocmemex-overview-collapsed'

// Every panel starts collapsed: the overview is a map of what needs
// attention, and the map itself should fit on one screen.
const DEFAULT_COLLAPSED = { issues: true, notes: true, tasks: true, docs: true, doneTasks: true }

function readCollapsed() {
  try {
    const raw = localStorage.getItem(PANEL_KEY)
    return raw ? JSON.parse(raw) : {}
  } catch {
    return {}
  }
}

const collapsed = ref({ ...DEFAULT_COLLAPSED, ...readCollapsed() })

function togglePanel(id) {
  collapsed.value = { ...collapsed.value, [id]: !collapsed.value[id] }
  try {
    localStorage.setItem(PANEL_KEY, JSON.stringify(collapsed.value))
  } catch { /* private mode */ }
}

function isCollapsed(id) {
  return Boolean(collapsed.value[id])
}

/* ── issue register ─────────────────────────────────────────────── */
const kind = ref('all')
const openIssueId = ref(null)
const anchorMode = ref('doc')
const anchorOpen = ref(false)
const selectedChunkId = ref(null)
const chunkQuery = ref('')
const draft = ref('')
const draftKind = ref('note')
const authorDraft = ref(notesStore.author)
const submitting = ref(false)
const docChunks = ref([])
const chunksLoading = ref(false)
const registerEl = ref(null)
const flashed = ref(false)

onMounted(() => {
  library.load()
  tasksStore.load()
  notesStore.load()
})

const stats = computed(() => library.overview?.stats || null)
const issues = computed(() => library.overview?.alerts || [])

/* ── first-run guidance ─────────────────────────────────────────── */
const FIRST_RUN_KEY = 'tocmemex-first-run-dismissed'
const firstRunDismissed = ref(false)
try {
  firstRunDismissed.value = localStorage.getItem(FIRST_RUN_KEY) === '1'
} catch { /* private mode */ }

function dismissFirstRun() {
  firstRunDismissed.value = true
  try {
    localStorage.setItem(FIRST_RUN_KEY, '1')
  } catch { /* private mode */ }
}

const filters = computed(() => {
  const all = issues.value
  const sum = (kinds) => all.filter((i) => kinds.includes(i.kind)).reduce((n, i) => n + i.count, 0)
  return [
    { value: 'all', label: '全部', count: all.reduce((n, i) => n + i.count, 0) },
    { value: 'missing', label: '缺失页', count: sum(['missing-pages']) },
    { value: 'unplaced', label: '未定位', count: sum(['unplaced']) },
    { value: 'duplicate', label: '重复编号', count: sum(['duplicate-heading']) },
    { value: 'failed', label: '解析失败', count: sum(['failed', 'parsing']) },
  ].filter((f) => f.value === 'all' || f.count)
})

const KIND_FILTER = {
  missing: ['missing-pages'],
  unplaced: ['unplaced'],
  duplicate: ['duplicate-heading'],
  failed: ['failed', 'parsing'],
}

const filteredIssues = computed(() => {
  if (kind.value === 'all') return issues.value
  const kinds = KIND_FILTER[kind.value] || []
  return issues.value.filter((i) => kinds.includes(i.kind))
})

const selectedChunk = computed(
  () => docChunks.value.find((c) => c.id === selectedChunkId.value) || null,
)

const filteredChunks = computed(() => {
  const q = chunkQuery.value.trim().toLowerCase()
  if (!q) return docChunks.value
  return docChunks.value.filter(
    (c) => c.path.toLowerCase().includes(q) || (c.page != null && `p${c.page}` === q),
  )
})

/** Notes posted from this exact issue row — the only thing that counts as a verdict. */
function issueNotes(issue) {
  return notes.value.filter((n) => n.issueId === issue.id)
}

/** Notes on the same document answering something else: context, not a verdict. */
function priorNotes(issue) {
  return notes.value.filter((n) => n.docId === issue.docId && !n.issueId).slice(0, 2)
}

function verdictLabel(issue) {
  const list = issueNotes(issue)
  return list.length ? `${list[0].author} 已判断` : null
}

/** Backend descriptions lead with the doc title, which the row already shows. */
function issueDetail(issue) {
  const prefix = `${issue.docTitle} · `
  return issue.desc.startsWith(prefix) ? issue.desc.slice(prefix.length) : issue.desc
}

const recentNotes = computed(() => notes.value.slice(0, 6))
const authors = computed(() => [...new Set(notes.value.map((n) => n.author))])
const openTasks = computed(() => tasksStore.tasks.filter((t) => t.status !== 'done' && t.status !== 'archived'))
const doneTasks = computed(() => tasksStore.tasks.filter((t) => t.status === 'done').slice(0, 3))
const recentDocs = computed(() => library.documents.slice(0, 6))

const TASK_STATUS = { open: '待办', doing: '进行中', blocked: '受阻', done: '已完成', archived: '已归档' }

function anchorText() {
  if (anchorMode.value === 'doc') return '整份文档'
  const chunk = selectedChunk.value
  return chunk ? `切片 #${chunk.index}` : '选择切片'
}

async function toggleIssue(issue) {
  if (openIssueId.value === issue.id) {
    resetComposer()
    return
  }
  openIssueId.value = issue.id
  draft.value = ''
  draftKind.value = 'note'
  chunkQuery.value = ''
  selectedChunkId.value = null
  anchorMode.value = issue.nodeId ? 'chunk' : 'doc'
  anchorOpen.value = Boolean(issue.nodeId)

  docChunks.value = []
  chunksLoading.value = true
  try {
    const data = await fetchDocumentChunks(issue.docId)
    docChunks.value = data.chunks
    if (issue.nodeId) {
      const match = data.chunks.find((c) => c.nodeId === issue.nodeId)
      selectedChunkId.value = match ? match.id : null
    }
  } catch (err) {
    ui.notify(`读取切片失败：${err.message}`)
  } finally {
    chunksLoading.value = false
  }
}

function resetComposer() {
  openIssueId.value = null
  draft.value = ''
  selectedChunkId.value = null
  chunkQuery.value = ''
  anchorOpen.value = false
}

function placeholderFor(issue) {
  if (!issue) return ''
  return anchorMode.value === 'doc'
    ? `例：${issue.title}已核对，原因是 OCR 页缺失，引用时跳过 P…`
    : '例：这一段 OCR 把表格切成了纯文本，列对应要看原页'
}

async function submit(issue) {
  if (!draft.value.trim()) return
  if (!notesStore.author.trim()) {
    ui.notify('先填上你的 agent 名字')
    return
  }
  submitting.value = true
  try {
    const chunk = anchorMode.value === 'chunk' ? selectedChunk.value : null
    await notesStore.post({
      kind: draftKind.value,
      body: draft.value.trim(),
      docId: issue.docId,
      nodeId: chunk ? chunk.nodeId : null,
      chunkId: chunk ? chunk.id : null,
      issueId: issue.id,
    })
    await library.refresh()
    ui.notify('已记录判断')
    resetComposer()
  } catch (err) {
    ui.notify(`提交失败：${err.message}`)
  } finally {
    submitting.value = false
  }
}

async function focusRegister() {
  if (isCollapsed('issues')) togglePanel('issues')
  registerEl.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  flashed.value = false
  await Promise.resolve()
  flashed.value = true
  setTimeout(() => (flashed.value = false), 1100)
}

function openDoc(docId, tab = 'quality') {
  router.push(`/doc/${docId}?tab=${tab}`)
}
</script>

<template>
  <section class="page">
    <div class="page-head">
      <div>
        <h1>概览</h1>
        <p>库的状态、待判断的问题、进行中的任务，以及 agent 留下的手记。看到问题就在那一行记录判断，下一位接手的人或 agent 会先读到这里。</p>
      </div>
      <div class="page-head-actions">
        <UiButton size="md" @click="router.push('/quality')">质量报告</UiButton>
      </div>
    </div>

    <section
      v-if="stats && stats.docs === 0 && !firstRunDismissed"
      class="panel first-run"
    >
      <div class="first-run-main">
        <div class="first-run-title">第一次使用 Toc Memex</div>
        <p>
          只有扫描件才需要 OCR Key，带文本层的 PDF 不需要；LLM 端点也不是必填：
          不配也能建库，只是目录规则判不准时没有 LLM 兜底，测试台问答和编修 agent 也不可用。
        </p>
      </div>
      <div class="first-run-actions">
        <UiButton variant="primary" size="sm" @click="router.push('/library')">去知识库上传</UiButton>
        <UiButton size="sm" @click="router.push('/settings')">去设置</UiButton>
        <button class="first-run-close" title="不再显示" @click="dismissFirstRun">×</button>
      </div>
    </section>

    <UiEmpty v-if="!stats" title="加载中…" hint="正在读取知识库状态。" />

    <template v-else>
      <!-- ── stats ── -->
      <div class="stats">
        <button class="stat stat-link" @click="router.push('/library')">
          <div class="stat-label">文档<span class="stat-go">→</span></div>
          <div class="stat-value">{{ stats.docs }}</div>
          <div class="stat-meta"><b>{{ stats.ready }}</b> 就绪 · <b>{{ stats.pending }}</b> 解析中 · <b>{{ stats.failed }}</b> 失败</div>
        </button>
        <button class="stat stat-link" @click="router.push('/quality')">
          <div class="stat-label">正文页<span class="stat-go">→</span></div>
          <div class="stat-value">{{ stats.contentPages }}</div>
          <div class="stat-meta">不含封面 / 目录等非正文页</div>
        </button>
        <button class="stat stat-link" @click="router.push('/search')">
          <div class="stat-label">切片<span class="stat-go">→</span></div>
          <div class="stat-value">{{ stats.chunks }}</div>
          <div class="stat-meta">BM25 索引已同步</div>
        </button>
        <button class="stat stat-link" :class="{ flash: flashed }" @click="focusRegister">
          <div class="stat-label">待处理问题<span class="stat-go">→</span></div>
          <div class="stat-value">{{ stats.issues }}<small>处</small></div>
          <div class="stat-meta"><b>{{ stats.issueGroups }}</b> 类 · <b>{{ stats.tracedIssues }}</b> 已判断 · {{ stats.failed }} 解析失败</div>
        </button>
      </div>

      <div class="overview-grid">
        <div class="overview-main">
          <!-- ── issues ── -->
          <section ref="registerEl" class="panel" :class="{ flashed }">
            <div class="panel-head panel-head-toggle" @click="togglePanel('issues')">
              <h3>待处理问题</h3>
              <span class="panel-sub">{{ filteredIssues.length }} / {{ issues.length }} 类</span>
              <span v-if="stats.tracedIssues" class="head-judged">{{ stats.tracedIssues }} 已判断</span>
              <span class="panel-spacer"></span>
              <UiSeg
                v-if="!isCollapsed('issues')"
                v-model="kind"
                :items="filters"
                aria-label="按问题类型筛选"
                @click.stop
              />
              <button class="collapse-btn" :title="isCollapsed('issues') ? '展开' : '收起'">
                <svg viewBox="0 0 24 24" :class="{ open: !isCollapsed('issues') }"><path d="m6 9 6 6 6-6"/></svg>
              </button>
            </div>

            <div v-show="!isCollapsed('issues')">
              <div v-for="issue in filteredIssues" :key="issue.id" class="issue" :class="{ open: openIssueId === issue.id }">
                <div class="issue-row" @click="toggleIssue(issue)">
                  <span class="sev" :class="issue.severity"></span>
                  <div class="issue-main">
                    <div class="issue-title">{{ issue.title }}<span class="n">{{ issue.count }} 处</span></div>
                    <div class="issue-doc">{{ issue.docTitle }}</div>
                    <div class="issue-desc">{{ issueDetail(issue) }}</div>
                  </div>
                  <div class="issue-side">
                    <span v-if="verdictLabel(issue)" class="trace">{{ verdictLabel(issue) }}</span>
                    <span v-else class="untraced">待判断</span>
                    <span class="chev">›</span>
                  </div>
                </div>

                <div v-if="openIssueId === issue.id" class="handle">
                  <div v-for="n in issueNotes(issue)" :key="n.id" class="judgment">
                    <div class="judgment-head">
                      <span class="who" :style="{ background: authorColor(n.author) }">{{ n.author }}</span>
                      <span class="judgment-kind">{{ KIND_LABELS[n.kind] || n.kind }}</span>
                      <span class="judgment-when">{{ relTime(n.createdAt) }}</span>
                    </div>
                    <p class="judgment-body">{{ n.body }}</p>
                  </div>

                  <div v-if="priorNotes(issue).length" class="ctx">
                    <b>这份文档的其他手记</b>
                    <span>与当前问题无关，仅作背景；提交后本行才会标「已判断」。</span>
                    <div v-for="n in priorNotes(issue)" :key="n.id" class="ctx-note">
                      <span class="who" :style="{ background: authorColor(n.author) }">{{ n.author }}</span>
                      <span class="ctx-text">{{ n.body }}</span>
                    </div>
                  </div>

                  <div class="composer">
                    <div v-if="!notesStore.author" class="composer-author">
                      <span>以</span>
                      <input
                        v-model="authorDraft"
                        placeholder="你的 agent 名字，如 claude-code"
                        @change="notesStore.setAuthor(authorDraft)"
                      >
                      <span>的名义记录</span>
                    </div>
                    <textarea
                      v-model="draft"
                      rows="2"
                      :placeholder="placeholderFor(issue)"
                      @keydown.ctrl.enter="submit(issue)"
                      @keydown.meta.enter="submit(issue)"
                    ></textarea>
                    <div class="composer-foot">
                      <select v-model="draftKind">
                        <option value="note">笔记</option>
                        <option value="pitfall">踩坑</option>
                        <option value="handoff">交接</option>
                      </select>
                      <button class="anchor-btn" :class="{ set: anchorOpen }" @click="anchorOpen = !anchorOpen">
                        锚点：{{ anchorText() }}
                      </button>
                      <span class="foot-spacer"></span>
                      <UiButton variant="quiet" size="sm" @click="openDoc(issue.docId)">打开文档</UiButton>
                      <UiButton variant="primary" size="sm" :disabled="submitting || !draft.trim() || (anchorMode === 'chunk' && !selectedChunkId)"
                        @click="submit(issue)"
                      >{{ submitting ? '提交中…' : '记录判断' }}</UiButton>
                    </div>

                    <div v-if="anchorOpen" class="anchor-box">
                      <div class="anchor-options">
                        <label :class="{ on: anchorMode === 'doc' }">
                          <input v-model="anchorMode" type="radio" value="doc">
                          整份文档<span>跨页的整体结论</span>
                        </label>
                        <label :class="{ on: anchorMode === 'chunk' }">
                          <input v-model="anchorMode" type="radio" value="chunk">
                          具体切片<span>精确到某一段正文</span>
                        </label>
                      </div>
                      <div v-if="anchorMode === 'chunk'" class="chunk-pick">
                        <input v-model="chunkQuery" placeholder="按标题或页码过滤">
                        <div class="chunk-scroll">
                          <div v-if="chunksLoading" class="chunk-empty">读取切片…</div>
                          <button
                            v-for="c in filteredChunks.slice(0, 200)"
                            :key="c.id"
                            class="chunk-opt"
                            :class="{ active: selectedChunkId === c.id }"
                            @click="selectedChunkId = c.id"
                          >
                            <span class="chunk-idx">{{ c.index }}</span>
                            <span class="chunk-path">{{ c.path || '(无标题)' }}</span>
                            <span v-if="c.page" class="chunk-page">P{{ c.page }}</span>
                          </button>
                          <div v-if="!chunksLoading && !filteredChunks.length" class="chunk-empty">没有匹配的切片</div>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              </div>

              <UiEmpty v-if="!filteredIssues.length" title="没有问题" hint="全部文档结构校验通过。" compact />

              <div class="panel-foot">
                <b>「已判断」</b> 只统计从这个问题行提交的手记，代表有人给出过判断；不代表问题已修好——库是只读的，修复要靠重新入库或人工改源文件。
              </div>
            </div>
          </section>

          <!-- ── notes ── -->
          <section class="panel">
            <div class="panel-head panel-head-toggle" @click="togglePanel('notes')">
              <h3>最近手记</h3>
              <span class="panel-sub">{{ notes.length }} 条 · {{ authors.length }} 个 agent</span>
              <span class="panel-spacer"></span>
              <UiButton variant="quiet" size="sm" @click.stop="router.push('/notes')">全部 →</UiButton>
              <button class="collapse-btn" :title="isCollapsed('notes') ? '展开' : '收起'">
                <svg viewBox="0 0 24 24" :class="{ open: !isCollapsed('notes') }"><path d="m6 9 6 6 6-6"/></svg>
              </button>
            </div>
            <div v-show="!isCollapsed('notes')">
              <div v-for="n in recentNotes" :key="n.id" class="note">
                <NoteCard :note="n" compact :replyable="false">
                  <template #actions>
                    <span class="anchor-chip">{{ n.issueId ? '问题判断' : n.chunkId ? '切片' : n.nodeId ? '目录节点' : '整份文档' }}</span>
                    <UiButton variant="quiet" size="sm" v-if="n.docId" @click="openDoc(n.docId, 'notes')">来源文档 →</UiButton>
                  </template>
                </NoteCard>
              </div>
              <UiEmpty v-if="!recentNotes.length" title="还没有手记" hint="在问题上留第一条判断。" compact />
            </div>
          </section>
        </div>

        <!-- ── side ── -->
        <aside class="overview-side">
          <section class="panel">
            <div class="panel-head panel-head-toggle" @click="togglePanel('tasks')">
              <h3>任务</h3>
              <span class="panel-sub">{{ tasksStore.counts.open }} 进行中 · {{ tasksStore.counts.done }} 已完成</span>
              <span class="panel-spacer"></span>
              <UiButton variant="quiet" size="sm" @click.stop="router.push('/tasks')">全部 →</UiButton>
              <button class="collapse-btn" :title="isCollapsed('tasks') ? '展开' : '收起'">
                <svg viewBox="0 0 24 24" :class="{ open: !isCollapsed('tasks') }"><path d="m6 9 6 6 6-6"/></svg>
              </button>
            </div>
            <div v-show="!isCollapsed('tasks')">
              <div v-for="task in openTasks" :key="task.id" class="task-line">
                <div class="task-top">
                  <span class="tstatus" :class="task.status">{{ TASK_STATUS[task.status] }}</span>
                  <span v-if="task.kind === 'session'" class="tkind">外部</span>
                  <span class="task-title">{{ task.title }}</span>
                </div>
                <div class="task-meta">{{ task.owner || '未指派' }} · 更新于 {{ relTime(task.updatedAt) }}</div>
                <div v-if="task.goal" class="task-goal">{{ task.goal }}</div>
              </div>
              <UiEmpty v-if="!openTasks.length" title="没有进行中的任务" hint="agent 通过 MCP 建任务后会出现在这里。" compact />

              <template v-if="doneTasks.length">
                <button class="sub-toggle" @click.stop="togglePanel('doneTasks')">
                  <svg viewBox="0 0 24 24" :class="{ open: !isCollapsed('doneTasks') }"><path d="m6 9 6 6 6-6"/></svg>
                  最近完成<span class="sub-count">{{ doneTasks.length }}</span>
                </button>
                <div v-show="!isCollapsed('doneTasks')">
                  <div v-for="task in doneTasks" :key="task.id" class="task-line done">
                    <div class="task-top">
                      <span class="tstatus done">{{ TASK_STATUS.done }}</span>
                      <span v-if="task.kind === 'session'" class="tkind">外部</span>
                      <span class="task-title">{{ task.title }}</span>
                    </div>
                    <div class="task-meta">{{ task.owner || '未指派' }} · {{ relTime(task.updatedAt) }}</div>
                  </div>
                </div>
              </template>

              <div class="panel-foot">
                任务来自活动层 <span class="mono">tasks</span> 表（跨 agent、跨会话），不是解析队列。
              </div>
            </div>
          </section>

          <section class="panel">
            <div class="panel-head panel-head-toggle" @click="togglePanel('docs')">
              <h3>最近入库</h3>
              <span class="panel-sub">按入库时间倒序</span>
              <span class="panel-spacer"></span>
              <UiButton variant="quiet" size="sm" @click.stop="router.push('/library')">全部 →</UiButton>
              <button class="collapse-btn" :title="isCollapsed('docs') ? '展开' : '收起'">
                <svg viewBox="0 0 24 24" :class="{ open: !isCollapsed('docs') }"><path d="m6 9 6 6 6-6"/></svg>
              </button>
            </div>
            <div v-show="!isCollapsed('docs')">
              <button v-for="doc in recentDocs" :key="doc.id" class="doc-line" @click="openDoc(doc.id, 'read')">
                <span class="doc-title">{{ doc.title }}</span>
                <span class="doc-meta">{{ doc.pages }} 页 · {{ doc.chunks }} 切片 · {{ relTime(doc.createdAt) }}</span>
              </button>
              <UiEmpty v-if="!recentDocs.length" title="还没有文档" hint="导入后出现在这里。" compact />
              <div class="panel-foot">
                口径：<b>入库时间</b>（<span class="mono">created_at</span>）。库内没有记录修改与浏览，所以不提供「最近修改」「最近查看」。
              </div>
            </div>
          </section>
        </aside>
      </div>
    </template>
  </section>
</template>

<style scoped>
/* ── stats ─────────────────────────────────────────────────── */
.stat-link {
  position: relative;
  text-align: left;
  width: 100%;
  cursor: pointer;
  transition: border-color .14s ease;
}
.stat-link:hover { border-color: var(--border-strong); }
.stat-link:hover .stat-go { opacity: 1; transform: none; }
.stat-value small {
  font-size: 12px;
  font-weight: 500;
  color: var(--faint);
  margin-left: 4px;
  letter-spacing: 0;
}
.stat-go {
  position: absolute;
  top: 14px;
  right: 14px;
  font-size: 11px;
  color: var(--accent);
  opacity: 0;
  transform: translateX(-2px);
  transition: opacity .14s ease, transform .14s ease;
}
.stat.flash { animation: flash 1s ease; }
@keyframes flash {
  0%, 100% { box-shadow: 0 0 0 0 transparent; }
  30% { box-shadow: 0 0 0 3px var(--accent-soft); border-color: var(--accent); }
}

/* ── first-run guidance ────────────────────────────────────── */
.first-run {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-top: 14px;
  border-color: color-mix(in srgb, var(--accent) 28%, var(--border));
  background: var(--accent-soft);
}
.first-run-main { flex: 1; min-width: 0; }
.first-run-title { font-size: 12.5px; font-weight: 600; color: var(--text); }
.first-run-main p {
  margin-top: 3px;
  font-size: 11.5px;
  line-height: 1.65;
  color: var(--muted);
}
.first-run-actions { display: flex; align-items: center; gap: 6px; flex: none; }
.first-run-close {
  border: 0;
  background: none;
  color: var(--faint);
  font-size: 16px;
  line-height: 1;
  padding: 4px;
  cursor: pointer;
}
.first-run-close:hover { color: var(--text); }

/* ── grid ──────────────────────────────────────────────────── */
.overview-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.55fr) minmax(330px, .85fr);
  gap: 14px;
  margin-top: 14px;
  align-items: start;
}
.overview-main { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
.overview-side { display: flex; flex-direction: column; gap: 14px; min-width: 0; }

/* ── panel chrome ──────────────────────────────────────────── */
.panel-head-toggle { cursor: pointer; user-select: none; }
.panel-head-toggle:hover h3 { color: var(--accent); }
.panel-spacer { flex: 1; }
.head-judged { font-size: 10.5px; font-weight: 500; color: var(--ok); }

.collapse-btn {
  flex: none;
  display: grid;
  place-items: center;
  width: 20px;
  height: 20px;
  border: 0;
  background: none;
  border-radius: var(--radius-sm);
  color: var(--faint);
}
.collapse-btn:hover { background: var(--surface-3); color: var(--text); }
.collapse-btn svg {
  width: 12px;
  height: 12px;
  fill: none;
  stroke: currentColor;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
  transition: transform .16s ease;
}
.collapse-btn svg.open { transform: rotate(180deg); }

.panel-foot {
  padding: 8px 16px;
  border-top: 1px solid var(--border);
  background: var(--surface-2);
  font-size: 10.5px;
  color: var(--faint);
  line-height: 1.55;
}
.panel-foot b { color: var(--muted); font-weight: 500; }


/* ── issue rows ────────────────────────────────────────────── */
.issue { border-bottom: 1px solid var(--border); }
.issue:last-child { border-bottom: 0; }
.issue-row {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  padding: 11px 16px;
  cursor: pointer;
}
.issue-row:hover { background: var(--surface-2); }
.issue.open .issue-row { background: var(--surface-2); }
.sev {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  margin-top: 6px;
  flex: none;
  background: var(--warn);
}
.sev.error { background: var(--danger); }
.issue-main { flex: 1; min-width: 0; }
.issue-title {
  font-size: 12px;
  font-weight: 500;
  display: flex;
  align-items: baseline;
  gap: 7px;
  flex-wrap: wrap;
}
.issue-title .n {
  font-size: 10.5px;
  color: var(--faint);
  font-weight: 400;
  font-variant-numeric: tabular-nums;
}
.issue-doc {
  font-size: 11px;
  color: var(--muted);
  margin-top: 2px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.issue-desc { font-size: 10.5px; color: var(--faint); margin-top: 3px; line-height: 1.5; }
.issue-side { display: flex; align-items: center; gap: 8px; flex: none; }
.trace {
  font-size: 10px;
  color: var(--ok);
  background: var(--ok-soft);
  border-radius: 999px;
  padding: 2px 7px;
}
.untraced {
  font-size: 10px;
  color: var(--muted);
  background: var(--surface-3);
  border-radius: 999px;
  padding: 2px 7px;
}
.chev { color: var(--faint); font-size: 13px; transition: transform .14s ease; }
.issue.open .chev { transform: rotate(90deg); }

/* ── expanded issue ────────────────────────────────────────── */
.handle {
  border-top: 1px dashed var(--border-strong);
  background: var(--surface-2);
  padding: 13px 16px 15px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.judgment { border-left: 2px solid var(--border-strong); padding-left: 11px; }
.judgment-head { display: flex; align-items: center; gap: 7px; }
.judgment-kind { font-size: 10px; color: var(--muted); }
.judgment-when { font-size: 10px; color: var(--faint); margin-left: auto; }
.judgment-body { margin: 4px 0 0; font-size: 11.5px; line-height: 1.65; }

.who {
  color: #fff;
  font-size: 9.5px;
  font-weight: 600;
  border-radius: 999px;
  padding: 2px 7px;
  flex: none;
}

.ctx {
  border: 1px solid var(--border);
  border-left: 2px solid var(--border-strong);
  border-radius: var(--radius-sm);
  background: var(--surface);
  padding: 8px 10px;
}
.ctx > b { font-size: 10.5px; font-weight: 600; color: var(--muted); }
.ctx > span { display: block; font-size: 10px; color: var(--faint); margin-top: 2px; }
.ctx-note { display: flex; gap: 7px; align-items: baseline; margin-top: 7px; }
.ctx-text {
  font-size: 10.5px;
  color: var(--muted);
  line-height: 1.5;
  display: -webkit-box;
  -webkit-line-clamp: 1;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

/* ── composer ──────────────────────────────────────────────── */
.composer { display: flex; flex-direction: column; gap: 6px; }
.composer-author { display: flex; align-items: center; gap: 6px; font-size: 10.5px; color: var(--faint); }
.composer-author input {
  flex: 1;
  height: 25px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
  padding: 0 8px;
  font-size: 11px;
  outline: none;
}
.composer-author input:focus { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-soft); }

.composer textarea {
  width: 100%;
  min-height: 54px;
  resize: vertical;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
  font-size: 11.5px;
  line-height: 1.6;
  outline: none;
}
.composer textarea:focus { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-soft); }

.composer-foot { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.composer-foot select {
  height: 25px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
  padding: 0 6px;
  font-size: 11px;
  outline: none;
}
.anchor-btn {
  border: 1px solid transparent;
  background: none;
  padding: 3px 6px;
  border-radius: var(--radius-sm);
  font-size: 10.5px;
  color: var(--faint);
}
.anchor-btn:hover { color: var(--text); background: var(--surface-3); }
.anchor-btn.set { color: var(--accent); }
.foot-spacer { flex: 1; }

.anchor-box {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
  padding: 9px 10px;
  display: flex;
  flex-direction: column;
  gap: 9px;
}
.anchor-options { display: flex; gap: 18px; }
.anchor-options label {
  display: flex;
  align-items: baseline;
  gap: 6px;
  font-size: 11.5px;
  color: var(--muted);
  cursor: pointer;
}
.anchor-options label.on { color: var(--text); font-weight: 500; }
.anchor-options input { accent-color: var(--accent); margin: 0; }
.anchor-options span { font-size: 10px; color: var(--faint); font-weight: 400; }

.chunk-pick { display: flex; flex-direction: column; gap: 6px; }
.chunk-pick input {
  width: 100%;
  height: 25px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
  padding: 0 8px;
  font-size: 11px;
  outline: none;
}
.chunk-pick input:focus { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-soft); }
.chunk-scroll {
  max-height: 156px;
  overflow: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}
.chunk-opt {
  display: flex;
  align-items: baseline;
  gap: 8px;
  width: 100%;
  padding: 5px 8px;
  border: 0;
  border-bottom: 1px solid var(--border);
  background: none;
  text-align: left;
  font-size: 11px;
  color: var(--muted);
}
.chunk-opt:last-child { border-bottom: 0; }
.chunk-opt:hover { background: var(--surface-2); }
.chunk-opt.active { background: var(--accent-soft); color: var(--accent); }
.chunk-idx {
  flex: none;
  font-family: var(--mono);
  font-size: 10px;
  color: var(--faint);
  font-variant-numeric: tabular-nums;
}
.chunk-opt.active .chunk-idx { color: var(--accent); }
.chunk-path { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chunk-page { flex: none; font-size: 10px; color: var(--faint); font-variant-numeric: tabular-nums; }
.chunk-empty { padding: 12px; text-align: center; font-size: 11px; color: var(--faint); }

/* ── notes ─────────────────────────────────────────────────── */
.note { padding: 11px 16px; border-bottom: 1px solid var(--border); }
.note:last-child { border-bottom: 0; }
/* 概览里只露前三行，点进去看全文 */
.note :deep(.body) {
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.anchor-chip {
  border: 1px solid var(--border);
  border-radius: 3px;
  padding: 1px 5px;
  font-size: 9.5px;
  color: var(--muted);
}

/* ── tasks ─────────────────────────────────────────────────── */
.tkind {
  font-size: 9.5px;
  padding: 1px 5px;
  border-radius: 3px;
  flex: none;
  color: var(--muted);
  background: var(--surface-2);
  border: 1px solid var(--border);
}
.task-line { padding: 10px 16px; border-bottom: 1px solid var(--border); }
.task-line:last-child { border-bottom: 0; }
.task-top { display: flex; align-items: baseline; gap: 7px; }
.task-title { font-size: 12px; font-weight: 500; line-height: 1.45; min-width: 0; }
.task-meta { font-size: 10.5px; color: var(--faint); margin-top: 3px; }
.task-goal { font-size: 10.5px; color: var(--muted); line-height: 1.5; margin-top: 3px; }
.task-line.done .task-title { font-weight: 400; color: var(--muted); }
.tstatus {
  font-size: 9.5px;
  padding: 1px 5px;
  border-radius: 3px;
  flex: none;
}
.tstatus.open { color: var(--accent); background: var(--accent-soft); }
.tstatus.doing { color: var(--warn); background: var(--warn-soft); }
.tstatus.blocked { color: var(--danger); background: var(--danger-soft); }
.tstatus.done { color: var(--ok); background: var(--ok-soft); }

.sub-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 8px 16px;
  border: 0;
  border-bottom: 1px solid var(--border);
  background: var(--surface-2);
  font-size: 10.5px;
  color: var(--faint);
  text-align: left;
}
.sub-toggle:hover { color: var(--text); }
.sub-toggle svg {
  width: 11px;
  height: 11px;
  fill: none;
  stroke: currentColor;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
  transition: transform .16s ease;
}
.sub-toggle svg.open { transform: rotate(180deg); }
.sub-count { margin-left: auto; font-variant-numeric: tabular-nums; }

/* ── docs ──────────────────────────────────────────────────── */
.doc-line {
  display: flex;
  flex-direction: column;
  gap: 2px;
  width: 100%;
  padding: 9px 16px;
  border: 0;
  border-bottom: 1px solid var(--border);
  background: none;
  text-align: left;
}
.doc-line:last-child { border-bottom: 0; }
.doc-line:hover { background: var(--surface-2); }
.doc-title {
  font-size: 11.5px;
  font-weight: 500;
  line-height: 1.4;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.doc-line:hover .doc-title { color: var(--accent); }
.doc-meta { font-size: 10px; color: var(--faint); font-variant-numeric: tabular-nums; }

@media (max-width: 1180px) {
  .overview-grid { grid-template-columns: 1fr; }
}
@media (max-width: 760px) {
  .issue-desc { white-space: normal; }
  .composer-foot { row-gap: 6px; }
}
</style>
