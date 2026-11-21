<script setup>
/**
 * Agent console: one question = one run. The left column starts a run and
 * lists history; the center is the step timeline; the right column shows the
 * selected step in full (search hits, a read section, citations, a note).
 *
 * The timeline is a view over persisted AgentStep rows, not live token
 * streaming: polling GET /api/runs/{id} every 700ms keeps it honest and makes
 * a finished run replayable after a reload.
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, useRouter } from 'vue-router'
import {
  cancelRun,
  fetchNotes,
  fetchResource,
  fetchRun,
  fetchRuns,
  originalFileUrl,
  startRun,
} from '@/api'
import { absTime, formatTokens, relTime } from '@/utils/format'
import BackToTop from '@/components/BackToTop.vue'
import NoteCard from '@/components/notes/NoteCard.vue'
import ResourcePreview from '@/components/doc/ResourcePreview.vue'
import UiButton from '@/components/ui/UiButton.vue'
import UiChip from '@/components/ui/UiChip.vue'
import UiEmpty from '@/components/ui/UiEmpty.vue'
import UiPanel from '@/components/ui/UiPanel.vue'
import UiSeg from '@/components/ui/UiSeg.vue'

const router = useRouter()

const question = ref('')
const allowNotes = ref(true)
const run = ref(null)
const runs = ref([])
const busy = ref(false)
const error = ref('')
const selectedSeq = ref(null)
const selectedRef = ref(null)
const followLatest = ref(true)
const chainCollapsed = ref(false)
const followUpId = ref(null)
const followUpQuestion = ref('')
const inputEl = ref(null)
const evidenceTab = ref('evidence')
const globalNotes = ref([])
const globalNotesLoading = ref(false)
const previewResource = ref(null)
const stopping = ref(false)

const EXAMPLES = [
  '8.3 的主要内容是什么？',
  '这份文档的关键结论有哪些？',
  '把 3.2 的步骤摘成表格',
]

// One run's tool-call and token ceilings. Neither has a UI yet; the server
// treats 0 tokens as its own default, so a future setting can override it.
const RUN_MAX_CALLS = 9_999
const RUN_TOKEN_BUDGET = 2_000_000

let timer = null
let historyTimer = null
let historyLoadedFor = null
let lastStepCount = -1
let lastChangeAt = Date.now()
const quiet = ref(false)

// Leaving for a citation and coming back should land where you left, not on an
// empty console. sessionStorage keeps it per tab without leaking across days.
const VIEW_KEY = 'tocmemex-agent-view'

function readSavedView() {
  try {
    const raw = sessionStorage.getItem(VIEW_KEY)
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}

function saveView() {
  try {
    if (!run.value) {
      sessionStorage.removeItem(VIEW_KEY)
      return
    }
    sessionStorage.setItem(VIEW_KEY, JSON.stringify({
      runId: run.value.id,
      selectedSeq: selectedSeq.value,
      selectedRef: selectedRef.value,
      evidenceTab: evidenceTab.value,
      chainCollapsed: chainCollapsed.value,
      scrollTop: document.querySelector('.content')?.scrollTop || 0,
    }))
  } catch {
    // Private mode may refuse storage; restoring is nice-to-have, not required.
  }
}

function clearSavedView() {
  try {
    sessionStorage.removeItem(VIEW_KEY)
  } catch {
    // ignore
  }
}

const TERMINAL = new Set(['done', 'failed', 'cancelled'])
const EXTERNAL = 'mcp'
const STALE_MS = 10 * 60 * 1000

/** An external run whose client stopped calling without run_end. */
function isStale(item) {
  if (!item || item.status !== 'running' || item.source !== EXTERNAL) return false
  const seen = Date.parse(item.lastSeenAt || item.createdAt || '')
  return Number.isFinite(seen) && Date.now() - seen > STALE_MS
}

function statusLabel(item) {
  if (!item) return ''
  if (isStale(item)) return '无活动'
  if (item.status === 'done') return '完成'
  if (item.status === 'failed') return '失败'
  if (item.status === 'cancelled') return '已取消'
  return '运行中'
}

function statusTone(item) {
  if (!item) return null
  if (item.status === 'failed' || item.status === 'cancelled') return 'danger'
  if (item.status === 'done') return 'ok'
  return 'accent'
}

const steps = computed(() => run.value?.steps || [])
const selectedStep = computed(
  () => steps.value.find((step) => step.seq === selectedSeq.value)
    || steps.value[steps.value.length - 1]
    || null,
)
const answerStep = computed(() =>
  [...steps.value].reverse().find((step) => step.type === 'answer') || null,
)
const citations = computed(
  () => answerStep.value?.result?.citations || run.value?.citations || [],
)

/** One card per ref; every quote under that ref stays visible. */
function groupCitations(list) {
  const groups = new Map()
  for (const citation of list || []) {
    const ref = citation.ref
    if (!groups.has(ref)) {
      groups.set(ref, { ...citation, quotes: [] })
    }
    const group = groups.get(ref)
    const quotes = citation.quotes?.length ? citation.quotes : [citation]
    for (const quote of quotes) {
      if (!group.quotes.some((item) => (
        item.quote === quote.quote
        && item.chunk_id === quote.chunk_id
        && item.resource_id === quote.resource_id
      ))) {
        group.quotes.push(quote)
      }
    }
  }
  return [...groups.values()]
}

const citationGroups = computed(() => groupCitations(citations.value))
function hasCitation(ref) {
  return citationGroups.value.some((citation) => citation.ref === ref)
}
const selectedCitation = computed(
  () => citationGroups.value.find((citation) => citation.ref === selectedRef.value) || null,
)
const noteSteps = computed(() => steps.value.filter((step) => step.type === 'note'))
// Every chunk the agent actually read this run, in read order. This trail is
// kept by the code from tool results, so it exists whether or not the model
// cited anything in its answer.
const viewedChunks = computed(() => {
  const seen = new Map()
  for (const step of steps.value) {
    if (step.type !== 'tool_result') continue
    if (step.name === 'read_section') {
      const docId = step.result?.doc_id
      for (const chunk of step.result?.chunks || []) {
        if (chunk?.chunk_id == null || seen.has(chunk.chunk_id)) continue
        seen.set(chunk.chunk_id, {
          kind: 'chunk',
          key: chunk.chunk_id,
          chunk_id: chunk.chunk_id,
          doc_id: docId,
          doc_title: step.result?.doc_title || '',
          heading_path: chunk.heading_path || step.result?.heading_path || '',
          page_start: chunk.page_start,
          page_end: chunk.page_end,
          text: chunk.text || '',
          seq: step.seq,
        })
      }
    } else if (
      step.name === 'read_resource'
      && step.result?.type === 'table'
      && step.result?.content
    ) {
      const key = `r:${step.result.doc_id}:${step.result.resource_id}`
      if (seen.has(key)) continue
      seen.set(key, {
        kind: 'resource',
        key,
        resource_id: step.result.resource_id,
        doc_id: step.result.doc_id,
        heading_path: step.result.heading_path || step.result.title || '',
        page_start: step.result.page,
        page_end: step.result.page,
        text: step.result.content || '',
        seq: step.seq,
      })
    }
  }
  return [...seen.values()]
})
// Used = the chunks the answer's sources point at. Optional by design; the
// trail above is the part that always exists.
const usedKeys = computed(() => {
  const keys = new Set()
  for (const citation of citations.value) {
    if (citation.resource_id) keys.add(`r:${citation.doc_id}:${citation.resource_id}`)
    else if (citation.chunk_id != null) keys.add(citation.chunk_id)
  }
  return keys
})
const usedCount = computed(() => viewedChunks.value.filter((item) => usedKeys.value.has(item.key)).length)
const evidenceTabs = computed(() => [
  { value: 'evidence', label: '证据' },
  { value: 'chunks', label: '本轮切片', count: viewedChunks.value.length },
  { value: 'notes', label: '本轮手记', count: noteSteps.value.length },
  { value: 'global-notes', label: '全局手记', count: globalNotes.value.length },
])
const answerText = computed(() => answerStep.value?.content || run.value?.answer || '')
// A long tool chain is noise once the answer is in: collapse it to the answer
// step. Before an answer exists there is nothing to hide, so keep it expanded.
const visibleSteps = computed(() => {
  if (!chainCollapsed.value || !answerStep.value) return steps.value
  return [answerStep.value]
})
const hiddenStepCount = computed(() => steps.value.length - visibleSteps.value.length)
const answerSegments = computed(() => {
  const text = answerText.value || ''
  const parts = []
  let last = 0
  for (const match of text.matchAll(/\[(\d+)\]/g)) {
    if (match.index > last) parts.push({ text: text.slice(last, match.index) })
    parts.push({ ref: Number(match[1]) })
    last = match.index + match[0].length
  }
  if (last < text.length) parts.push({ text: text.slice(last) })
  return parts
})
const selectedHits = computed(() => {
  if (selectedStep.value?.name !== 'search') return []
  return selectedStep.value?.result?.results || []
})
const selectedSection = computed(() => {
  if (selectedStep.value?.name !== 'read_section') return null
  const result = selectedStep.value?.result
  return result && !result.error ? result : null
})
const tokenTotal = computed(() => {
  const usage = run.value?.usage || {}
  return usage.total || 0
})
// usage.total sums every turn's prompt, so it is the billed amount, not the
// context size. The largest single prompt is what the model actually held.
const peakPromptTokens = computed(() => {
  let peak = 0
  for (const step of steps.value) {
    if (step.type !== 'llm') continue
    peak = Math.max(peak, Number(step.tokens?.prompt) || 0)
  }
  return peak
})
const runLabel = computed(() => {
  if (!run.value) return ''
  if (run.value.status === 'running') {
    return quiet.value || isStale(run.value) ? '无活动' : '运行中'
  }
  if (run.value.status === 'queued') return '排队中'
  if (run.value.status === 'failed') return '失败'
  if (run.value.status === 'cancelled') return '已取消'
  return '已完成'
})
const isExternal = computed(() => run.value?.source === EXTERNAL)
const sourceLabel = computed(() => {
  if (!run.value || !isExternal.value) return ''
  return `MCP · ${run.value.client || '会话'}`
})
const runTone = computed(() => {
  if (!run.value) return ''
  if (run.value.status === 'failed') return 'danger'
  if (run.value.status === 'done') return 'ok'
  return 'accent'
})

function stopPolling() {
  if (timer) {
    clearTimeout(timer)
    timer = null
  }
}

/** Auto-selection: show the answer once there is one, otherwise the last step. */
function preferredSeq(stepsList) {
  const answer = [...(stepsList || [])].reverse().find((step) => step.type === 'answer')
  return answer?.seq ?? stepsList?.[stepsList.length - 1]?.seq ?? null
}

async function refresh(id) {
  // Cancel any poll already scheduled for this view before we schedule a new
  // one. refresh() is called from several places (start, openRun, restoreView,
  // stopRun) as well as from the poll timer itself; without this, a direct
  // call while a timer is pending would start a second, independent
  // self-rescheduling loop and double the request rate.
  stopPolling()
  try {
    const data = await fetchRun(id)
    run.value = data.run
    const count = data.run.steps?.length ?? 0
    if (count !== lastStepCount) {
      lastStepCount = count
      lastChangeAt = Date.now()
    }
    if (followLatest.value || selectedSeq.value == null) {
      selectedSeq.value = preferredSeq(data.run.steps)
    }
    if (TERMINAL.has(data.run.status)) {
      quiet.value = false
      if (historyLoadedFor !== data.run.finishedAt) {
        historyLoadedFor = data.run.finishedAt
        await loadHistory()
      }
    } else if (isStale(data.run)) {
      // An external client can vanish without calling run_end. Stop the
      // poll loop rather than hammering the DB forever; opening the run
      // again from history resumes it.
      quiet.value = true
      stopPolling()
    } else {
      quiet.value = false
      // Poll fast while steps are arriving, then back off for a session
      // that is thinking (or waiting) between calls.
      const quietMs = Date.now() - lastChangeAt
      timer = setTimeout(() => refresh(id), quietMs > 30_000 ? 3000 : 700)
    }
  } catch (err) {
    error.value = err.message
    stopPolling()
  }
}

async function loadHistory(quiet = false) {
  try {
    runs.value = (await fetchRuns(20)).runs
  } catch (err) {
    if (!quiet) error.value = err.message
  }
}

async function start() {
  const text = question.value.trim()
  if (!text || busy.value) return
  busy.value = true
  error.value = ''
  stopPolling()
  try {
    const data = await startRun({
      question: text,
      allowNotes: allowNotes.value,
      priorRunId: followUpId.value,
      maxCalls: RUN_MAX_CALLS,
      maxTokens: RUN_TOKEN_BUDGET,
    })
    run.value = data.run
    question.value = ''
    selectedSeq.value = null
    selectedRef.value = null
    followLatest.value = true
    chainCollapsed.value = false
    followUpId.value = null
    followUpQuestion.value = ''
    quiet.value = false
    evidenceTab.value = 'evidence'
    lastStepCount = -1
    lastChangeAt = Date.now()
    await loadHistory()
    refresh(data.run.id)
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = false
  }
}

async function openRun(item) {
  stopPolling()
  error.value = ''
  followLatest.value = true
  selectedSeq.value = null
  selectedRef.value = null
  chainCollapsed.value = false
  evidenceTab.value = 'evidence'
  quiet.value = false
  lastStepCount = -1
  lastChangeAt = Date.now()
  run.value = item
  await refresh(item.id)
}

/** Reopen the run and selection left behind when navigating to a citation. */
async function restoreView(saved) {
  try {
    const data = await fetchRun(saved.runId)
    run.value = data.run
    selectedSeq.value = saved.selectedSeq ?? preferredSeq(data.run.steps)
    selectedRef.value = saved.selectedRef ?? null
    evidenceTab.value = saved.evidenceTab || 'evidence'
    chainCollapsed.value = Boolean(saved.chainCollapsed)
    followLatest.value = false
    lastStepCount = data.run.steps?.length ?? 0
    lastChangeAt = Date.now()
    await nextTick()
    const box = document.querySelector('.content')
    if (box && saved.scrollTop) box.scrollTop = saved.scrollTop
    if (!TERMINAL.has(data.run.status) && !isStale(data.run)) refresh(data.run.id)
  } catch {
    // The run is gone (deleted DB, another server); fall back to an empty console.
    clearSavedView()
  }
}

function startFollowUp() {
  if (!run.value) return
  followUpId.value = run.value.id
  followUpQuestion.value = run.value.question
  question.value = ''
  nextTick(() => inputEl.value?.focus())
}

function clearFollowUp() {
  followUpId.value = null
  followUpQuestion.value = ''
}

function useExample(text) {
  question.value = text
  nextTick(() => inputEl.value?.focus())
}

function newQuestion() {
  stopPolling()
  clearSavedView()
  run.value = null
  question.value = ''
  error.value = ''
  selectedSeq.value = null
  selectedRef.value = null
  chainCollapsed.value = false
  followUpId.value = null
  followUpQuestion.value = ''
  evidenceTab.value = 'evidence'
  nextTick(() => inputEl.value?.focus())
}

function selectStep(step) {
  selectedSeq.value = step.seq
  selectedRef.value = null
  evidenceTab.value = 'evidence'
  followLatest.value = false
}

/** Fold the tool chain away; keep the answer in view and selected. */
function toggleChain() {
  chainCollapsed.value = !chainCollapsed.value
  if (chainCollapsed.value && answerStep.value) {
    selectedSeq.value = answerStep.value.seq
    selectedRef.value = null
    evidenceTab.value = 'evidence'
    followLatest.value = false
  }
}

function selectCitation(ref) {
  selectedRef.value = ref
  evidenceTab.value = 'evidence'
  followLatest.value = false
  selectedSeq.value = answerStep.value?.seq ?? selectedSeq.value
}

function typeLabel(step) {
  return {
    start: '开始',
    llm: 'LLM',
    tool_call: '调用',
    tool_result: '结果',
    note: '手记',
    info: '提示',
    answer: '回答',
    error: '错误',
    done: '完成',
  }[step.type] || step.type
}

function toolSummary(step) {
  const result = step.result
  if (step.type === 'tool_call') return compactArgs(step.args)
  if (!result) return ''
  if (result.error) return result.error
  if (step.name === 'search') return `${result.results?.length ?? 0} 条命中`
  if (step.name === 'ask') return `${result.citations?.length ?? 0} 条引用`
  if (step.name === 'read_section') {
    return `${result.heading_path || result.title || ''} · ${pageRange(result.page_start, result.page_end)}`
  }
  if (step.name === 'get_toc') return result.title || '目录'
  if (step.name === 'get_document') return result.title || result.doc_id || '文档'
  if (step.name === 'list_documents') {
    const count = Array.isArray(result) ? result.length : result.documents?.length
    return `${count ?? 0} 份文档`
  }
  if (step.name === 'overview') return `${result.documents?.length ?? 0} 份文档概览`
  if (step.name === 'list_collections') return `${result.collections?.length ?? 0} 个分区`
  if (step.name === 'list_resources') return `${result.resources?.length ?? 0} 个资源`
  if (step.name === 'mark_used') return `标记 ${result.marked ?? 0} 条`
  if (step.name === 'add_note') return result.kind || '手记'
  if (step.name === 'final_answer') {
    if (result.accepted) return result.insufficient ? '拒答已提交' : `用过 ${result.citations ?? 0} 个切片`
    return result.error || '未通过'
  }
  return ''
}

function compactArgs(args) {
  if (!args || typeof args !== 'object') return ''
  return Object.entries(args)
    .map(([key, value]) => `${key}=${typeof value === 'string' ? value : JSON.stringify(value)}`)
    .join(' · ')
}

function pageRange(start, end) {
  if (start == null) return '页码未知'
  if (end == null || end === start) return `第 ${start + 1} 页`
  return `第 ${start + 1}–${end + 1} 页`
}

function pretty(value) {
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

function openCitationDoc(citation) {
  router.push({
    path: `/doc/${citation.doc_id}`,
    query: {
      tab: 'chunks',
      ...(citation.chunk_id != null ? { chunk: citation.chunk_id } : {}),
    },
  })
}

function openCitationPage(citation) {
  const page = citation.page_start == null ? NaN : Number(citation.page_start)
  const suffix = Number.isFinite(page) ? `#page=${page + 1}` : ''
  window.open(originalFileUrl(citation.doc_id) + suffix, '_blank', 'noopener')
}

async function loadGlobalNotes() {
  if (globalNotesLoading.value || globalNotes.value.length) return
  globalNotesLoading.value = true
  try {
    const data = await fetchNotes({ limit: 50 })
    globalNotes.value = data.notes || []
  } catch {
    globalNotes.value = []
  } finally {
    globalNotesLoading.value = false
  }
}

watch(evidenceTab, (tab) => {
  if (tab === 'global-notes') loadGlobalNotes()
})

function openNoteDoc(note) {
  if (note?.docId) router.push(`/doc/${note.docId}`)
}

async function openResourceById(docId, resourceId) {
  if (!docId || !resourceId) return
  try {
    const data = await fetchResource(docId, resourceId)
    const resource = data.resource
    previewResource.value = {
      ...resource,
      pageLabel: resource.page == null ? '页码未知' : `第 ${resource.page + 1} 页`,
      meta: resource.type,
    }
  } catch {
    // A missing resource should not break the console; the step JSON remains.
  }
}

function openResourceQuote(quote, citation) {
  return openResourceById(
    quote.doc_id || citation.doc_id,
    quote.resource_id,
  )
}

function openViewed(item) {
  if (item.kind === 'resource') return openResourceQuote(item, item)
  return openCitationDoc(item)
}

function openStepResource(step) {
  const result = step.result || {}
  if (result.image_meta) {
    previewResource.value = {
      id: step.args?.resource_id,
      type: result.type || 'figure',
      title: result.title || step.args?.resource_id,
      page: result.page,
      pageLabel: result.page == null ? '页码未知' : `第 ${result.page + 1} 页`,
      content: result.content || null,
      image: result.image_meta.url,
    }
    return
  }
  openResourceById(result.doc_id, step.args?.resource_id)
}

function imageStatus(result) {
  if (!result?.image_meta) return ''
  const error = result.image_include_error || result.image_error
  if (error) return `图片未发送：${error}`
  if (result.image_note) return result.image_note
  return '图片已发送给模型'
}

function openHit(hit) {
  router.push({
    path: `/doc/${hit.doc_id}`,
    query: {
      tab: 'chunks',
      ...(hit.chunk_id != null ? { chunk: hit.chunk_id } : {}),
    },
  })
}

function stepTone(step) {
  if (step.type === 'error') return 'danger'
  if (step.type === 'tool_result' && step.result?.error) return 'danger'
  if (step.type === 'answer') return 'accent'
  if (step.type === 'done') return 'ok'
  if (step.type === 'note') return 'warn'
  return ''
}

async function stopRun() {
  if (!run.value || stopping.value) return
  stopping.value = true
  // Stop the poll loop now: the run is being cancelled, so the next poll we
  // schedule would only race the cancel and re-fetch a run we are ending.
  stopPolling()
  try {
    await cancelRun(run.value.id)
    await refresh(run.value.id)
  } catch (err) {
    error.value = err?.message || '停止失败'
  } finally {
    stopping.value = false
  }
}

onMounted(() => {
  loadHistory()
  // Restore the run a citation jump interrupted, then keep polling it if live.
  const saved = readSavedView()
  if (saved?.runId) restoreView(saved)
  // External MCP sessions open runs from another process; refresh the
  // history so they appear without a manual reload.
  historyTimer = setInterval(() => loadHistory(true), 5000)
})
onBeforeRouteLeave(() => {
  saveView()
})
onBeforeUnmount(() => {
  stopPolling()
  if (historyTimer) clearInterval(historyTimer)
})
</script>

<template>
  <section class="page agent-page">
    <div class="page-head">
      <div>
        <h1>测试台</h1>
        <p>看 agent 在你的档案里检索、阅读、回答，并留下手记。</p>
      </div>
      <div class="page-head-actions">
        <UiChip v-if="run" :tone="runTone">{{ runLabel }}</UiChip>
        <UiChip v-if="sourceLabel" tone="plain">{{ sourceLabel }}</UiChip>
        <UiChip v-if="run?.endpoint">{{ run.endpoint }} · {{ run.model }}</UiChip>
        <UiChip v-if="run && !isExternal" tone="plain">
          累计 {{ formatTokens(tokenTotal) }} tokens<template v-if="peakPromptTokens"> · 峰值上下文 {{ formatTokens(peakPromptTokens) }}</template>
        </UiChip>
        <UiButton
          v-if="run && !isExternal && (run.status === 'running' || run.status === 'queued')"
          size="sm"
          :disabled="stopping"
          @click="stopRun"
        >
          {{ stopping ? '停止中…' : '停止' }}
        </UiButton>
      </div>
    </div>

    <section class="agent-composer">
      <p class="agent-model-hint">模型沿用「编修 agent」的端点设置（未绑定则用默认端点），可在「设置 → 编修 agent」中修改；单轮预算 200 万 tokens，统计为各轮输入累计，不是当前上下文大小。</p>
      <div v-if="followUpId" class="follow-up-chip">
        <span>承接上一轮：{{ followUpQuestion }}</span>
        <button type="button" @click="clearFollowUp">取消</button>
      </div>

      <textarea
        ref="inputEl"
        v-model="question"
        class="agent-question"
        rows="2"
        :placeholder="followUpId ? '追问上一轮的结论…' : '向档案提问，例如：8.3 的主要内容是什么？'"
        @keydown.ctrl.enter.prevent="start"
      />

      <div v-if="!run && !followUpId" class="agent-examples">
        <span>试试：</span>
        <button
          v-for="example in EXAMPLES"
          :key="example"
          type="button"
          class="agent-example"
          @click="useExample(example)"
        >
          {{ example }}
        </button>
      </div>

      <div class="agent-composer-foot">
        <label class="agent-check">
          <input v-model="allowNotes" type="checkbox">
          允许写手记
        </label>
        <span class="agent-hint">Ctrl+Enter 开始</span>
        <UiButton
          variant="primary"
          :disabled="busy || !question.trim()"
          @click="start"
        >
          {{ busy ? '启动中…' : '开始' }}
        </UiButton>
      </div>
      <p v-if="error" class="agent-error">{{ error }}</p>
    </section>

    <div
      class="agent-workspace"
      :class="{ 'is-empty': !run, 'no-history': !runs.length }"
    >
      <UiPanel v-if="runs.length" class="agent-history" title="历史" :sub="runs.length + ' 轮'" flush>
        <template #actions>
          <button
            type="button"
            class="agent-new"
            title="新提问"
            aria-label="新提问"
            @click="newQuestion"
          >
            +
          </button>
        </template>
        <div class="agent-history-list">
          <button
            v-for="item in runs"
            :key="item.id"
            type="button"
            class="run-row"
            :class="{ active: run?.id === item.id }"
            @click="openRun(item)"
          >
            <span class="run-question">{{ item.question }}</span>
            <span class="run-meta">
              <span class="agent-dot" :class="statusTone(item)" />
              {{ relTime(item.createdAt) }}
              <template v-if="item.source === EXTERNAL"> · MCP</template>
            </span>
          </button>
        </div>
      </UiPanel>

      <UiPanel v-if="run" class="agent-trace-panel" flush>
        <template #head>
          <div class="trace-head-copy">
            <h3>{{ run.question }}</h3>
            <div class="trace-meta">
              <span>{{ absTime(run.createdAt) }}</span>
            </div>
          </div>
        </template>
        <template #actions>
          <UiButton
            v-if="answerStep && steps.length > 1"
            size="sm"
            variant="ghost"
            @click="toggleChain"
          >
            {{ chainCollapsed ? '展开过程' : '只看回答' }}
          </UiButton>
          <UiButton
            v-if="run.status === 'done' && !isExternal"
            size="sm"
            @click="startFollowUp"
          >
            追问
          </UiButton>
        </template>

        <div class="agent-trace">
          <button
            v-if="chainCollapsed && hiddenStepCount"
            type="button"
            class="trace-fold"
            @click="toggleChain"
          >
            <span>已折叠 {{ hiddenStepCount }} 步执行过程</span>
            <span class="trace-fold-action">展开</span>
          </button>
          <div
            v-for="step in visibleSteps"
            :key="step.seq"
            class="trace-step"
            :class="{ active: selectedStep?.seq === step.seq }"
            @click="selectStep(step)"
          >
            <span class="trace-rail">
              <span class="trace-dot" :class="`tone-${stepTone(step)}`" />
            </span>
            <div class="trace-main">
              <div class="trace-head">
                <span class="trace-type">{{ typeLabel(step) }}</span>
                <span v-if="step.name" class="trace-name">{{ step.name }}</span>
                <span v-if="step.elapsedMs != null" class="trace-time">{{ step.elapsedMs }} ms</span>
              </div>

              <div v-if="step.type === 'llm'" class="trace-text">
                {{ step.content || '（本轮没有文字说明）' }}
              </div>
              <div v-else-if="step.type === 'tool_call'" class="trace-text trace-mono">
                {{ toolSummary(step) }}
              </div>
              <div
                v-else-if="step.type === 'tool_result'"
                class="trace-text"
                :class="{ bad: step.result?.error }"
              >
                {{ toolSummary(step) }}
              </div>
              <div v-else-if="step.type === 'note'" class="trace-note">
                <UiChip size="sm" tone="warn">{{ step.result?.kind || step.args?.kind || 'note' }}</UiChip>
                <span>{{ step.result?.body || step.args?.body }}</span>
              </div>
              <div v-else-if="step.type === 'info'" class="trace-text muted">
                {{ step.content }}
              </div>
              <div v-else-if="step.type === 'answer'" class="trace-answer">
                <template v-for="(part, index) in answerSegments" :key="index">
                  <span v-if="part.text">{{ part.text }}</span>
                  <button
                    v-else-if="hasCitation(part.ref)"
                    type="button"
                    class="cite-ref"
                    :class="{ active: selectedRef === part.ref }"
                    @click.stop="selectCitation(part.ref)"
                  >
                    [{{ part.ref }}]
                  </button>
                  <span v-else>[{{ part.ref }}]</span>
                </template>
                <div v-if="step.result?.insufficient" class="trace-badge">
                  证据不足，已拒答
                </div>
              </div>
              <div v-else-if="step.type === 'error'" class="trace-text bad">
                {{ step.content }}
              </div>
              <div v-else-if="step.type === 'start'" class="trace-text muted">
                {{ step.content }}
              </div>
              <div v-else-if="step.type === 'done'" class="trace-text muted">
                共 {{ steps.length }} 步<template v-if="!isExternal"> · 累计 {{ formatTokens(tokenTotal) }} tokens<template v-if="peakPromptTokens">（峰值上下文 {{ formatTokens(peakPromptTokens) }}）</template></template>
              </div>
            </div>
          </div>
          <div v-if="!steps.length" class="agent-empty">正在启动 agent…</div>
        </div>
      </UiPanel>

      <UiPanel v-if="run" class="agent-evidence" title="证据" flush>
        <template #actions>
          <UiSeg v-model="evidenceTab" :items="evidenceTabs" size="sm" aria-label="证据视图" />
        </template>

        <div class="agent-evidence-body">
          <template v-if="evidenceTab === 'chunks'">
            <div v-if="!viewedChunks.length" class="agent-empty">这一轮还没有读过切片。</div>
            <template v-else>
              <div class="inspector-block">
                <div class="inspector-label">
                  本轮读过 {{ viewedChunks.length }} 个切片<template v-if="usedCount"> · {{ usedCount }} 个标注为用过</template>
                </div>
              </div>
              <button
                v-for="item in viewedChunks"
                :key="item.key"
                type="button"
                class="hit-card"
                @click="openViewed(item)"
              >
                <div class="hit-title">
                  {{ item.doc_title || item.heading_path || (item.kind === 'resource' ? item.resource_id : `chunk ${item.chunk_id}`) }}
                </div>
                <div class="inspector-meta">
                  <UiChip v-if="usedKeys.has(item.key)" size="sm" tone="warn">用过</UiChip>
                  {{ item.kind === 'resource' ? '表格' : `chunk ${item.chunk_id}` }}
                  <template v-if="item.heading_path && item.heading_path !== item.doc_title"> · {{ item.heading_path }}</template>
                  · {{ pageRange(item.page_start, item.page_end) }}
                </div>
                <div class="hit-snippet">{{ (item.text || '').slice(0, 160) }}</div>
              </button>
            </template>
          </template>

          <template v-else-if="evidenceTab === 'notes'">
            <div v-if="!noteSteps.length" class="agent-empty">这一轮没有写手记。</div>
            <div v-for="step in noteSteps" :key="step.seq" class="note-card">
              <div class="note-head">
                <UiChip size="sm" tone="warn">{{ step.result?.kind || 'note' }}</UiChip>
              </div>
              <div class="note-body">{{ step.result?.body || step.args?.body }}</div>
              <div class="inspector-meta">
                {{ step.result?.doc_id || '未锚定文档' }}
                <template v-if="step.result?.node_id"> · {{ step.result.node_id }}</template>
              </div>
            </div>
          </template>

          <template v-else-if="evidenceTab === 'global-notes'">
            <div v-if="globalNotesLoading" class="agent-empty">正在载入手记…</div>
            <div v-else-if="!globalNotes.length" class="agent-empty">还没有全局手记。</div>
            <div v-for="note in globalNotes" :key="note.id" class="note-card">
              <NoteCard
                :note="note"
                :replyable="false"
                show-doc
                @open-doc="openNoteDoc"
              />
            </div>
          </template>

          <template v-else-if="selectedCitation">
            <div class="inspector-block">
              <button type="button" class="ev-back" @click="selectedRef = null">← 用过的切片</button>
              <div class="inspector-label">用过的切片 [{{ selectedCitation.ref }}]</div>
              <div class="inspector-title">{{ selectedCitation.doc_title }}</div>
              <div class="inspector-meta">
                {{ selectedCitation.heading_path }} · {{ pageRange(selectedCitation.page_start, selectedCitation.page_end) }}
              </div>
              <div v-for="(quote, index) in selectedCitation.quotes" :key="index" class="quote-row">
                <blockquote class="inspector-quote">{{ quote.quote }}</blockquote>
                <div class="inspector-meta">
                  <template v-if="quote.resource_id">
                    表格 · {{ quote.resource_title || quote.resource_id }}
                    · {{ quote.resource_page == null ? '页码未知' : `第 ${quote.resource_page + 1} 页` }}
                  </template>
                  <template v-else>
                    chunk {{ quote.chunk_id }} · {{ quote.heading_path }}
                    · {{ pageRange(quote.page_start, quote.page_end) }}
                  </template>
                </div>
                <UiButton
                  v-if="quote.resource_id"
                  size="sm"
                  @click="openResourceQuote(quote, selectedCitation)"
                >
                  查看表格
                </UiButton>
              </div>
              <div class="inspector-actions">
                <UiButton
                  v-if="!selectedCitation.resource_id || selectedCitation.chunk_id != null"
                  size="sm"
                  @click="openCitationDoc(selectedCitation)"
                >
                  跳到切片
                </UiButton>
                <UiButton
                  v-if="selectedCitation.page_start != null"
                  size="sm"
                  @click="openCitationPage(selectedCitation)"
                >
                  原文第 {{ selectedCitation.page_start + 1 }} 页
                </UiButton>
              </div>
            </div>
          </template>

          <template v-else-if="selectedStep?.type === 'answer'">
            <div v-if="!citations.length" class="agent-empty">
              {{ isExternal ? '外部 agent 不提供可核验的引用，请以其工具调用为准。' : '答案没有标注用过的切片（不影响答案）。可切到「本轮切片」查看读过哪些。' }}
            </div>
            <div v-else class="cite-list">
              <button
                v-for="citation in citationGroups"
                :key="citation.ref"
                type="button"
                class="cite-card"
                :class="{ active: selectedRef === citation.ref }"
                @click="selectCitation(citation.ref)"
              >
                <span class="cite-ref-label">[{{ citation.ref }}]</span>
                <span class="cite-body">
                  <span class="cite-card-title">
                    {{ citation.resource_id ? (citation.resource_title || citation.doc_title) : citation.doc_title }}
                  </span>
                  <span class="inspector-meta">
                    <template v-if="citation.resource_id">
                      表格 · {{ citation.resource_page == null ? '页码未知' : `第 ${citation.resource_page + 1} 页` }}
                    </template>
                    <template v-else>
                      {{ citation.heading_path }} · {{ pageRange(citation.page_start, citation.page_end) }}
                    </template>
                    <template v-if="citation.quotes.length > 1"> · {{ citation.quotes.length }} 条原文</template>
                  </span>
                </span>
              </button>
            </div>
          </template>

          <template v-else-if="selectedHits.length">
            <div class="inspector-block">
              <div class="inspector-label">检索命中</div>
            </div>
            <button
              v-for="hit in selectedHits"
              :key="hit.chunk_id"
              type="button"
              class="hit-card"
              @click="openHit(hit)"
            >
              <div class="hit-title">{{ hit.heading_path || hit.title || hit.doc_id }}</div>
              <div class="inspector-meta">
                {{ pageRange(hit.page_start, hit.page_end) }}
                <template v-if="hit.score != null"> · score {{ Number(hit.score).toFixed(2) }}</template>
              </div>
              <div class="hit-snippet">{{ hit.snippet }}</div>
            </button>
          </template>

          <template v-else-if="selectedSection">
            <div class="inspector-block">
              <div class="inspector-label">读取章节</div>
              <div class="inspector-title">{{ selectedSection.heading_path || selectedSection.title }}</div>
              <div class="inspector-meta">
                {{ pageRange(selectedSection.page_start, selectedSection.page_end) }}
                · {{ selectedSection.char_count || 0 }} 字
              </div>
            </div>
            <div v-if="selectedSection.chunks?.length" class="chunk-list">
              <div
                v-for="chunk in selectedSection.chunks"
                :key="chunk.chunk_id"
                class="chunk-card"
              >
                <div class="chunk-head">
                  <span class="cite-ref-label">chunk {{ chunk.chunk_id }}</span>
                  <span class="inspector-meta">
                    {{ pageRange(chunk.page_start, chunk.page_end) }} · {{ chunk.heading_path }}
                  </span>
                </div>
                <div v-if="chunk.figures?.length" class="chunk-figures">
                  <UiChip
                    v-for="figure in chunk.figures"
                    :key="figure.resource_id"
                    size="sm"
                    tone="warn"
                  >
                    图 · {{ figure.title || figure.resource_id }} · {{ pageRange(figure.page, figure.page) }}
                  </UiChip>
                </div>
                <pre class="inspector-pre">{{ chunk.text }}</pre>
              </div>
            </div>
            <div v-else class="inspector-block">
              <pre class="inspector-pre">{{ selectedSection.content }}</pre>
            </div>
          </template>

          <template v-else-if="selectedStep?.name === 'read_resource'">
            <div class="inspector-block">
              <div class="inspector-label">资源 · {{ selectedStep.args?.resource_id }}</div>
              <div class="inspector-title">{{ selectedStep.result?.title || selectedStep.args?.resource_id }}</div>
              <div class="inspector-meta">
                {{ selectedStep.result?.type || 'resource' }}
                · {{ pageRange(selectedStep.result?.page, selectedStep.result?.page) }}
              </div>
              <img
                v-if="selectedStep.result?.image_meta"
                class="resource-thumb"
                :src="selectedStep.result.image_meta.url"
                :alt="selectedStep.result.title"
                @click="openStepResource(selectedStep)"
              >
              <div v-if="selectedStep.result?.image_meta" class="inspector-meta">
                {{ imageStatus(selectedStep.result) }}
              </div>
              <div class="inspector-actions">
                <UiButton size="sm" @click="openStepResource(selectedStep)">查看资源</UiButton>
              </div>
            </div>
          </template>

          <template v-else-if="selectedStep">
            <div class="inspector-block">
              <div class="inspector-label">{{ typeLabel(selectedStep) }} · {{ selectedStep.name || '' }}</div>
              <pre class="inspector-pre">{{ pretty(selectedStep.result ?? selectedStep.args ?? selectedStep.content) }}</pre>
            </div>
          </template>

          <div v-else class="agent-empty">点执行过程里的任意一步，在这里查看完整证据。</div>
        </div>
      </UiPanel>

      <div v-if="!run" class="agent-empty-canvas">
        <UiEmpty
          title="还没有提问"
          hint="agent 会在档案里检索、读章节、给出带引用的回答。每一步都会留在执行过程里，可以回放。"
        />
      </div>
    </div>

    <BackToTop />
    <ResourcePreview
      v-if="previewResource"
      :resource="previewResource"
      @close="previewResource = null"
    />
  </section>
</template>

<style scoped>
.agent-page { max-width: 1600px; }
.agent-composer { margin-bottom: 14px; padding: 12px 14px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); transition: border-color .12s var(--ease), box-shadow .12s var(--ease); }
.agent-composer:focus-within { border-color: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 16%, transparent); }
.agent-model-hint { margin-bottom: 7px; font-size: 10.5px; line-height: 1.5; color: var(--faint); }
.agent-question {
  display: block;
  width: 100%;
  min-height: 58px;
  max-height: 180px;
  padding: 2px 0;
  border: 0;
  outline: none;
  background: transparent;
  color: var(--text);
  font: inherit;
  font-size: 13.5px;
  line-height: 1.65;
  resize: vertical;
}
.agent-question::placeholder { color: var(--faint); }
.agent-question:focus { box-shadow: none; }

.agent-examples { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-top: 10px; }
.agent-examples > span { font-size: 11px; color: var(--faint); }
.agent-example { height: 24px; padding: 0 9px; border: 1px solid var(--pill-line); border-radius: 999px; background: var(--pill); color: var(--muted); font-size: 11px; }
.agent-example:hover { border-color: color-mix(in srgb, var(--accent) 45%, var(--pill-line)); color: var(--accent); }

.agent-composer-foot { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-top: 8px; padding-top: 10px; border-top: 1px solid var(--border); }
.agent-composer-foot :deep(.ui-btn) { margin-left: auto; }
.agent-check { display: inline-flex; align-items: center; gap: 6px; font-size: 11.5px; color: var(--muted); }
.agent-check input { accent-color: var(--accent); }
.agent-hint { font-size: 11px; color: var(--faint); }
.agent-error { margin-top: 8px; font-size: 11.5px; color: var(--danger); line-height: 1.6; }

.follow-up-chip { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; padding: 6px 9px; border: 1px solid color-mix(in srgb, var(--accent) 25%, var(--border)); border-radius: var(--radius-sm); background: var(--accent-soft); font-size: 11px; color: var(--accent); }
.follow-up-chip span { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.follow-up-chip button { border: 0; background: transparent; color: var(--accent); font-size: 11px; padding: 0; }

.agent-workspace { display: grid; grid-template-columns: 220px minmax(0, 1fr) 340px; gap: 14px; align-items: stretch; }
.agent-workspace.no-history { grid-template-columns: minmax(0, 1fr) 340px; }
.agent-workspace.is-empty { grid-template-columns: 220px minmax(0, 1fr); }
.agent-workspace.is-empty.no-history { grid-template-columns: minmax(0, 1fr); }

.agent-history-list { display: flex; flex-direction: column; }
.agent-new { display: grid; place-items: center; width: 24px; height: 24px; border: 0; border-radius: 6px; background: transparent; color: var(--muted); font-size: 16px; line-height: 1; }
.agent-new:hover { background: var(--pill-hover); color: var(--text); }

.run-row { display: flex; flex-direction: column; gap: 5px; width: 100%; padding: 10px 14px; border: 0; border-bottom: 1px solid var(--border); background: transparent; color: var(--text); text-align: left; }
.run-row:last-child { border-bottom: 0; }
.run-row:hover { background: var(--surface-2); }
.run-row.active { background: var(--accent-soft); }
.run-row.active .run-question { color: var(--accent); }
.run-question { display: -webkit-box; overflow: hidden; font-size: 12px; line-height: 1.5; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
.run-meta { display: flex; align-items: center; gap: 6px; font-size: 10.5px; color: var(--faint); }
.agent-dot { width: 5px; height: 5px; flex: none; border-radius: 50%; background: var(--faint); }
.agent-dot.ok { background: var(--ok); }
.agent-dot.accent { background: var(--accent); }
.agent-dot.danger { background: var(--danger); }

.trace-head-copy { flex: 1; min-width: 0; }
.agent-trace-panel :deep(.ui-panel-head) { align-items: flex-start; padding: 12px 16px; }
.trace-head-copy h3 { font-size: 13.5px; font-weight: 600; line-height: 1.5; overflow-wrap: anywhere; text-wrap: balance; }
.trace-meta { display: flex; align-items: center; gap: 6px; margin-top: 5px; font-size: 10.5px; color: var(--faint); }

.agent-trace { padding: 6px 0 10px; }
.trace-fold { display: flex; align-items: center; gap: 8px; width: 100%; padding: 9px 16px; border: 0; border-bottom: 1px solid var(--border); background: transparent; color: var(--faint); font-size: 11px; text-align: left; }
.trace-fold:hover { background: var(--surface-2); color: var(--muted); }
.trace-fold-action { color: var(--accent); }
.trace-step { display: grid; grid-template-columns: 32px minmax(0, 1fr); padding: 9px 16px 9px 0; cursor: pointer; }
.trace-step:hover { background: var(--surface-2); }
.trace-step.active { background: var(--accent-soft); }
.trace-rail { position: relative; display: flex; justify-content: center; }
.trace-rail::before { content: ""; position: absolute; top: 0; bottom: -18px; left: 50%; width: 1px; background: var(--border); transform: translateX(-.5px); }
.trace-step:last-child .trace-rail::before { display: none; }
.trace-dot { position: relative; z-index: 1; width: 9px; height: 9px; margin-top: 4px; flex: none; border: 1.5px solid var(--faint); border-radius: 50%; background: var(--surface); }
.trace-dot.tone-accent { border-color: var(--accent); background: var(--accent); }
.trace-dot.tone-ok { border-color: var(--ok); }
.trace-dot.tone-warn { border-color: var(--warn); }
.trace-dot.tone-danger { border-color: var(--danger); }
.trace-main { min-width: 0; padding-right: 4px; }
.trace-head { display: flex; align-items: baseline; gap: 7px; }
.trace-type { font-size: 10.5px; font-weight: 600; letter-spacing: .04em; color: var(--muted); }
.trace-name { font-family: var(--mono); font-size: 10.5px; color: var(--accent); }
.trace-time { margin-left: auto; font-family: var(--mono); font-size: 10px; color: var(--faint); font-variant-numeric: tabular-nums; }
.trace-text { margin-top: 3px; font-size: 12.5px; line-height: 1.7; white-space: pre-wrap; overflow-wrap: anywhere; }
.trace-text.muted { color: var(--muted); }
.trace-mono { font-family: var(--mono); font-size: 11.5px; color: var(--muted); }
.trace-text.bad { color: var(--danger); }
.trace-note { display: flex; align-items: flex-start; gap: 8px; margin-top: 4px; font-size: 12.5px; line-height: 1.7; }
.trace-note span:last-child { white-space: pre-wrap; overflow-wrap: anywhere; }
.trace-answer { margin-top: 3px; font-size: 13px; line-height: 1.85; white-space: pre-wrap; overflow-wrap: anywhere; }
.trace-badge { display: inline-block; margin-top: 6px; padding: 2px 8px; border-radius: 999px; background: var(--warn-soft); color: var(--warn); font-size: 10.5px; }
.cite-ref { display: inline; margin: 0 1px; padding: 0 3px; border: 0; border-radius: 3px; background: var(--accent-soft); color: var(--accent); font-family: var(--mono); font-size: 10.5px; line-height: 1.3; vertical-align: 1px; }
.cite-ref:hover, .cite-ref.active { background: var(--accent); color: #fff; }
[data-theme="dark"] .cite-ref.active { color: #0f1115; }

.inspector-block { padding: 12px 14px; border-bottom: 1px solid var(--border); }
.inspector-block:last-child { border-bottom: 0; }
.ev-back { display: inline-flex; align-items: center; margin-bottom: 8px; padding: 0; border: 0; background: none; color: var(--accent); font-size: 11px; }
.inspector-label { margin-bottom: 6px; font-size: 10px; font-weight: 600; letter-spacing: .05em; text-transform: uppercase; color: var(--faint); }
.inspector-title { font-size: 12.5px; font-weight: 600; line-height: 1.5; }
.inspector-meta { display: block; margin-top: 4px; font-size: 10.5px; color: var(--faint); line-height: 1.5; }
.inspector-quote { margin: 9px 0 0; padding-left: 10px; border-left: 2px solid var(--border-strong); color: var(--muted); font-size: 11.5px; line-height: 1.7; white-space: pre-wrap; }
.inspector-actions { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 10px; }
.inspector-pre { max-height: 340px; margin: 8px 0 0; padding: 10px; overflow: auto; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface-2); font-family: var(--mono); font-size: 11px; line-height: 1.6; white-space: pre-wrap; overflow-wrap: anywhere; }

.cite-list { display: flex; flex-direction: column; }
.cite-card { display: flex; gap: 9px; width: 100%; padding: 10px 14px; border: 0; border-bottom: 1px solid var(--border); background: transparent; color: var(--text); text-align: left; }
.cite-card:last-child { border-bottom: 0; }
.cite-card:hover { background: var(--surface-2); }
.cite-card.active { background: var(--accent-soft); }
.cite-ref-label { flex: none; padding-top: 1px; color: var(--accent); font-family: var(--mono); font-size: 10.5px; }
.cite-body { min-width: 0; }
.cite-card-title { display: block; font-size: 12px; font-weight: 600; line-height: 1.45; }
.cite-card .inspector-meta { margin-top: 3px; }

.hit-card { display: block; width: 100%; padding: 10px 14px; border: 0; border-bottom: 1px solid var(--border); background: transparent; color: var(--text); text-align: left; }
.hit-card:last-child { border-bottom: 0; }
.hit-card:hover { background: var(--surface-2); }
.hit-title { font-size: 12px; font-weight: 600; line-height: 1.45; }
.hit-card .inspector-meta { margin-top: 3px; }
.hit-snippet { display: -webkit-box; margin-top: 5px; overflow: hidden; color: var(--muted); font-size: 11.5px; line-height: 1.65; -webkit-box-orient: vertical; -webkit-line-clamp: 3; }

.note-card { padding: 12px 14px; border-bottom: 1px solid var(--border); }
.note-card:last-child { border-bottom: 0; }
.quote-row { margin-top: 10px; }
.quote-row + .quote-row { padding-top: 10px; border-top: 1px solid var(--border); }
.chunk-list { margin-top: 6px; }
.chunk-card { padding: 10px 14px; border-bottom: 1px solid var(--border); }
.chunk-card:last-child { border-bottom: 0; }
.chunk-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 7px; }
.chunk-head .inspector-meta { margin-top: 0; }
.chunk-figures { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 6px; }
.chunk-card .inspector-pre { max-height: 240px; margin-top: 6px; }
.resource-thumb { display: block; max-width: 100%; max-height: 260px; margin-top: 9px; border: 1px solid var(--border); border-radius: var(--radius-sm); cursor: zoom-in; object-fit: contain; }
.note-head { display: flex; align-items: center; gap: 7px; margin-bottom: 6px; }
.note-body { font-size: 12px; line-height: 1.7; white-space: pre-wrap; overflow-wrap: anywhere; }
.note-card .inspector-meta { margin-top: 6px; }

.agent-empty { padding: 18px 14px; color: var(--faint); font-size: 11.5px; line-height: 1.7; }
.agent-empty-canvas { display: flex; align-items: center; justify-content: center; min-height: 280px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); }

@media (max-width: 1240px) {
  .agent-workspace { grid-template-columns: 200px minmax(0, 1fr); }
  .agent-workspace.no-history { grid-template-columns: minmax(0, 1fr); }
  .agent-workspace.is-empty { grid-template-columns: 200px minmax(0, 1fr); }
  .agent-workspace.is-empty.no-history { grid-template-columns: minmax(0, 1fr); }
  .agent-evidence { grid-column: 1 / -1; }
}
@media (max-width: 900px) {
  .agent-workspace,
  .agent-workspace.no-history,
  .agent-workspace.is-empty,
  .agent-workspace.is-empty.no-history { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 560px) {
  .agent-page .page-head { flex-direction: column; align-items: flex-start; }
  .agent-composer-foot :deep(.ui-btn) { width: 100%; margin-left: 0; }
}
</style>
