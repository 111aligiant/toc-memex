<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import { useWorkspaceStore } from '@/stores/workspace'
import { useNotesStore } from '@/stores/notes'
import { useUiStore } from '@/stores/ui'
import { originalFileUrl, exportDocument } from '@/api'
import { statusLabel } from '@/utils/format'
import { copyText } from '@/utils/clipboard'
import { downloadBlob } from '@/utils/download'
import ReadTab from '@/components/doc/ReadTab.vue'
import TocTab from '@/components/doc/TocTab.vue'
import ChunksTab from '@/components/doc/ChunksTab.vue'
import QualityTab from '@/components/doc/QualityTab.vue'
import InfoTab from '@/components/doc/InfoTab.vue'
import NotesTab from '@/components/doc/NotesTab.vue'
import BackToTop from '@/components/BackToTop.vue'
import UiChip from '@/components/ui/UiChip.vue'
import UiEmpty from '@/components/ui/UiEmpty.vue'
import UiButton from '@/components/ui/UiButton.vue'

const route = useRoute()
const router = useRouter()
const workspace = useWorkspaceStore()
const notesStore = useNotesStore()
const ui = useUiStore()
const { tab, document: doc } = storeToRefs(workspace)

onMounted(applyRoute)

watch(() => route.params.id, (id) => {
  if (id && id !== workspace.docId) applyRoute()
})

// Deep links to the same document (?chunk=… / ?node=… / ?tab=…) don't remount this view.
watch(() => [route.query.chunk, route.query.node, route.query.tab], ([chunk, node, wantedTab]) => {
  if (!workspace.docId || route.params.id !== workspace.docId) return
  if (chunk != null) workspace.openChunk({ chunkId: Number(chunk) })
  else if (node) workspace.openChunk({ nodeId: String(node) })
  else if (wantedTab && wantedTab !== tab.value) tab.value = wantedTab
})

/** Load the doc, then honour ?chunk=<id> / ?node=<id> (deep link from 手记) or ?tab=. */
async function applyRoute() {
  if (route.params.id !== workspace.docId) await workspace.load(route.params.id)
  const { chunk, node, tab: wantedTab } = route.query
  if (chunk != null) workspace.openChunk({ chunkId: Number(chunk) })
  else if (node) workspace.openChunk({ nodeId: String(node) })
  else if (wantedTab) tab.value = wantedTab
}

watch(() => workspace.docId, (id) => {
  if (id) notesStore.loadDoc(id)
})

watch(tab, (value) => {
  if (value !== 'notes') notesStore.focusAnchor(null)
  // A ?chunk= / ?node= link belongs to the 切片 tab. Drop it when leaving that
  // tab, otherwise the route watcher above re-opens the chunk on the query
  // change and the tab snaps straight back to 切片.
  const leavesDeepLink = value !== 'chunks'
    && (route.query.chunk != null || route.query.node != null)
  if (route.query.tab === value && !leavesDeepLink) return
  const { chunk, node, ...query } = route.query
  router.replace({ query: { ...query, tab: value } })
})

/** Picking 切片 from the tab bar means "all slices", not the last node's scope. */
function setTab(id) {
  if (id === 'chunks') workspace.chunkScopeId = null
  tab.value = id
}

const tabs = computed(() => [
  { id: 'read', label: '正文' },
  { id: 'toc', label: '目录' },
  { id: 'chunks', label: '切片' },
  { id: 'quality', label: '质量' },
  { id: 'info', label: '信息' },
  { id: 'notes', label: '手记', count: notesStore.docNotes.length },
])

/** ✎ markers in 正文 / 目录 / 切片 land here on the matching anchor. */
function openNotes(anchor) {
  notesStore.focusAnchor(anchor)
  tab.value = 'notes'
}

/**
 * 只读服务不提供重新索引。与其弹一句无用的提示，不如把带真实 doc_id 的
 * CLI 命令复制到剪贴板——这才是用户拿到按钮后真正要做的下一步。
 */
async function copyReindexCommand() {
  if (!doc.value) return
  const command = `python -m app.cli reindex ${doc.value.id}`
  const ok = await copyText(command)
  ui.notify(ok ? '已复制重新索引命令，到终端粘贴执行' : '复制失败：浏览器拒绝了剪贴板访问')
}

/** Jump to the library filtered to this document's collection. */
function openCollection() {
  router.push({
    path: '/library',
    query: { collection: doc.value?.collectionId || 'unassigned' },
  })
}

/** Open the original file in a new tab, optionally at a 0-based page. */
function openOriginal(page = null) {
  if (!doc.value) return
  const provenance = workspace.provenance?.current
  if (provenance && !provenance.file.exists) {
    ui.notify('原始文件已不在原路径，无法打开')
    return
  }
  // `Number(null)` is 0, which would send a page-less open to #page=1.
  const pageNumber = page == null ? NaN : Number(page)
  const suffix = Number.isInteger(pageNumber) && pageNumber >= 0
    ? `#page=${pageNumber + 1}`
    : ''
  window.open(originalFileUrl(doc.value.id) + suffix, '_blank', 'noopener')
}

/**
 * 导出解析原始数据。按钮文案跟着服务端算出的 exportKind 走：OCR 文档导
 * 厂商原始响应，文本层文档导原件 + 现算逐页结果——两者都不假装是对方。
 */
const exporting = ref(false)

const exportKind = computed(() => workspace.exportKind || '')

const exportLabel = computed(() => {
  if (exporting.value) return '导出中…'
  if (workspace.loading) return '导出'
  if (exportKind.value === 'ocr') return '导出 OCR 原始数据'
  if (exportKind.value === 'text-layer') return '导出解析数据'
  return '无可导出的解析数据'
})

const exportTitle = computed(() => {
  if (exportKind.value === 'ocr') return 'ZIP：厂商原始响应 raw.jsonl + 归一化 pages.json + 书签 + manifest'
  if (exportKind.value === 'text-layer') return '该文档未调用 OCR。ZIP：原件 + 按文本层现算的 pages.json + 书签 + manifest'
  if (workspace.loading) return '文档加载中…'
  return '原始文件缺失或类型不支持解析，无法导出'
})

async function exportParse() {
  if (!doc.value || exporting.value || !exportKind.value) return
  exporting.value = true
  try {
    const { blob, filename, kind } = await exportDocument(doc.value.id)
    downloadBlob(blob, filename)
    ui.notify(kind === 'ocr' ? '已导出 OCR 原始数据' : '已导出解析数据（该文档未走 OCR）')
  } catch (err) {
    ui.notify(`导出失败：${err.message}`)
  } finally {
    exporting.value = false
  }
}

const metaLine = computed(() => {
  const d = doc.value
  if (!d) return []
  return [
    `${d.page_count ?? '—'} 页`,
    `${workspace.stats?.nodes ?? '—'} 节点`,
    `${workspace.stats?.chunks ?? '—'} 切片`,
    `覆盖 ${workspace.stats?.coveredPages ?? '—'}/${workspace.stats?.contentPages ?? '—'}`,
  ]
})
</script>

<template>
  <section v-if="doc" class="page">
    <div class="doc-head">
      <div class="doc-head-main">
        <div class="row-inline mb-7">
          <UiButton variant="quiet" size="sm" @click="router.push('/library')">← 知识库</UiButton>
          <span class="faint">/</span>
          <span class="faint">{{ doc.filename }}</span>
        </div>
        <h1>{{ doc.title }}</h1>
        <div class="doc-meta">
          <template v-for="(item, index) in metaLine" :key="index">
            <span>{{ item }}</span><i v-if="index < metaLine.length - 1 || doc.errors + doc.warnings" class="sep"></i>
          </template>
          <span v-if="doc.errors || doc.warnings" :class="doc.errors ? 'danger' : 'warn'">{{ doc.errors }} 错误 · {{ doc.warnings }} 警告</span>
          <i class="sep"></i>
          <UiChip
            size="sm"
            as="button"
            :title="doc.collectionId ? '在知识库中查看该分区' : '这份文档还没有归入分区'"
            @click="openCollection"
          >{{ doc.collectionName || '未分区' }}</UiChip>
          <i class="sep"></i>
          <span class="status" :class="doc.status"><span class="dot"></span>{{ statusLabel(doc.status) }}</span>
        </div>
      </div>
      <div class="doc-head-actions">
        <UiButton size="md" title="只读服务不能重索引；点击复制 CLI 命令" @click="copyReindexCommand">复制重索引命令</UiButton>
        <UiButton size="md" :disabled="!exportKind || exporting" :title="exportTitle" @click="exportParse">{{ exportLabel }}</UiButton>
        <UiButton
          size="md"
          @click="openOriginal(workspace.selectedChunk?.pageStart ?? workspace.selectedNode?.page_start ?? null)"
        >
          打开原文
        </UiButton>
      </div>
    </div>

    <div v-if="doc.status === 'failed'" class="notice error">
      解析失败：{{ doc.error || '未知错误' }}。可重试解析或查看原始日志。
    </div>

    <div class="tabs">
      <button
        v-for="item in tabs"
        :key="item.id"
        class="tab"
        :class="{ active: tab === item.id }"
        @click="setTab(item.id)"
      >
        {{ item.label }}<span v-if="item.count" class="tab-count">{{ item.count }}</span>
      </button>
    </div>

    <ReadTab v-if="tab === 'read'" @open-notes="openNotes" />
    <TocTab v-else-if="tab === 'toc'" @open-notes="openNotes" />
    <ChunksTab v-else-if="tab === 'chunks'" @open-notes="openNotes" />
    <QualityTab v-else-if="tab === 'quality'" />
    <NotesTab v-else-if="tab === 'notes'" />
    <InfoTab v-else />

    <BackToTop />
  </section>

  <section v-else class="page">
    <UiEmpty v-if="workspace.loading" title="加载中…" />
    <UiEmpty v-else :title="workspace.error === 'document not found' ? '文档不存在' : workspace.error || '文档不存在'" hint="返回知识库选择其他文档。" />
  </section>
</template>
