<script setup>
import { computed, onMounted, shallowRef, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import DocTable from '@/components/DocTable.vue'
import IngestNote from '@/components/notes/IngestNote.vue'
import CollectionPanel from '@/components/CollectionPanel.vue'
import CollectionPicker from '@/components/CollectionPicker.vue'
import UploadDialog from '@/components/UploadDialog.vue'
import UiSeg from '@/components/ui/UiSeg.vue'
import { assignDocuments, deleteDocument } from '@/api'
import { useCollectionsStore } from '@/stores/collections'
import { useLibraryStore } from '@/stores/library'
import { useUiStore } from '@/stores/ui'
import { useUploadStore } from '@/stores/upload'
import { coverageText, fileClass, fileExt, statusLabel } from '@/utils/format'
import UiChip from '@/components/ui/UiChip.vue'
import UiEmpty from '@/components/ui/UiEmpty.vue'
import { copyText } from '@/utils/clipboard'
import UiButton from '@/components/ui/UiButton.vue'

const route = useRoute()
const router = useRouter()
const library = useLibraryStore()
const collections = useCollectionsStore()
const ui = useUiStore()
const uploadStore = useUploadStore()
const {
  query, status, issuesOnly, source, collection, layout, filteredDocs, documents,
} = storeToRefs(library)
const selectedIds = shallowRef([])
const panelOpen = shallowRef(false)
const pickerOpen = shallowRef(false)
const uploadOpen = shallowRef(false)
const uploadPrefill = shallowRef(null)
// The list renders before onMounted's fetch resolves; gate the empty state on
// it so "档案库还是空的" doesn't flash on every visit.
const listReady = shallowRef(false)

/** Upload targets the partition currently being viewed, when there is one. */
const uploadTarget = computed(() =>
  collection.value && !['all', 'unassigned'].includes(collection.value)
    ? collection.value
    : '',
)

const layoutOptions = [
  { value: 'table', label: '表格' },
  { value: 'cards', label: '卡片' },
]

onMounted(async () => {
  const wanted = route.query.collection
  if (wanted) collection.value = String(wanted)
  const loaded = Promise.all([library.load(), collections.load()])
  try {
    await loaded
  } finally {
    listReady.value = true
  }
  // A failed ingest in 解析队列 sends the user back here with the original
  // file and every upload choice prefilled; open the dialog for them.
  const retry = uploadStore.consumeRetry()
  if (retry) {
    uploadPrefill.value = retry
    uploadOpen.value = true
  }
})

// Keep the filter in the URL so a document page can link back to its section.
watch(collection, (value) => {
  const next = value === 'all' ? undefined : value
  if ((route.query.collection || undefined) !== next) {
    router.replace({ query: { ...route.query, collection: next } })
  }
})

function toggleSelect(id) {
  selectedIds.value = selectedIds.value.includes(id)
    ? selectedIds.value.filter((item) => item !== id)
    : [...selectedIds.value, id]
}

async function assign(collectionId) {
  pickerOpen.value = false
  if (!selectedIds.value.length) return
  const target = collectionId ? collections.byId(collectionId) : null
  try {
    const result = await assignDocuments({
      docIds: selectedIds.value,
      collectionId,
    })
    await Promise.all([library.refresh(), collections.refresh()])
    if (result.updated) {
      ui.notify(
        `已移入 ${target ? target.name : '未分区'} · ${result.updated} 份`,
      )
    } else {
      ui.notify(`所选文档已在${target ? target.name : '未分区'}，没有改动`)
    }
    clearSelection()
  } catch (error) {
    ui.notify(error.message)
  }
}

/**
 * Deleting the collection the library is currently filtered to would leave
 * the filter pointing at a dead id: every document disappears from the list
 * and the select renders blank. Fall back to "all" in that case.
 */
async function onCollectionsChanged() {
  await Promise.all([library.refresh(), collections.load()])
  const stillExists = collections.items.some((item) => item.id === collection.value)
  if (collection.value !== 'all' && collection.value !== 'unassigned' && !stillExists) {
    collection.value = 'all'
  }
}

/**
 * 只读服务不提供批量重索引。复制每份选中文档的 CLI 命令，
 * 用户粘贴到终端一次跑完——比弹一句提示有用得多。
 */
async function copyReindexCommands() {
  if (!selectedIds.value.length) return
  const commands = selectedIds.value
    .map((id) => `python -m app.cli reindex ${id}`)
    .join('\n')
  const ok = await copyText(commands)
  if (!ok) {
    ui.notify('复制失败：浏览器拒绝了剪贴板访问')
    return
  }
  ui.notify(`已复制 ${selectedIds.value.length} 条重索引命令`)
}

function clearSelection() {
  selectedIds.value = []
}

/**
 * Delete one or more documents. Each document is its own request so a
 * failure on one (a file held open, a race with ingest) does not abort the
 * rest; the toast reports how many actually went.
 */
async function removeDocuments(ids) {
  if (!ids.length) return
  const label = ids.length === 1 ? '这份文档' : `${ids.length} 份文档`
  const ok = window.confirm(
    `删除${label}？\n\n目录树、切片、手记和原始文件会一并删除，无法撤销。`,
  )
  if (!ok) return
  const failed = []
  for (const id of ids) {
    try {
      await deleteDocument(id)
    } catch (error) {
      failed.push(error.message)
    }
  }
  clearSelection()
  await Promise.all([library.refresh(), collections.refresh()])
  if (failed.length) {
    ui.notify(`已删除 ${ids.length - failed.length} 份，${failed.length} 份失败：${failed[0]}`)
  } else {
    ui.notify(`已删除 ${ids.length} 份文档`)
  }
}

function removeDocument(doc) {
  removeDocuments([doc.id])
}

async function onUploadDone() {
  await Promise.all([library.refresh(), collections.refresh()])
}

function closeUpload() {
  uploadOpen.value = false
  uploadPrefill.value = null
}
</script>

<template>
  <section class="page">
    <div class="page-head">
      <div>
        <h1>知识库</h1>
        <p>{{ filteredDocs.length }} / {{ documents.length }} 份文档</p>
      </div>
      <div class="page-head-actions">
        <UiButton variant="primary" size="sm" @click="uploadOpen = true">上传</UiButton>
        <UiButton size="sm" @click="panelOpen = true">管理分区</UiButton>
      </div>
    </div>

    <div class="panel">
      <div class="toolbar">
        <div class="search-box">
          <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
          <input v-model="query" type="search" placeholder="筛选标题、关键词…">
        </div>
        <select v-model="collection" aria-label="按分区筛选">
          <option value="all">全部分区</option>
          <option value="unassigned">未分区</option>
          <option v-for="item in collections.items" :key="item.id" :value="item.id">
            {{ item.name }}
          </option>
        </select>
        <select v-model="status">
          <option value="all">全部状态</option>
          <option value="ready">就绪</option>
          <option value="parsing">解析中</option>
          <option value="indexing">解析中</option>
          <option value="failed">失败</option>
          <option value="pending">排队中</option>
        </select>
        <select v-model="source">
          <option value="all">全部入库来源</option>
          <option value="ocr">用过 OCR</option>
          <option value="text">未用 OCR（文本层）</option>
          <option value="llm">用过 LLM</option>
        </select>
        <label class="check-label"><input v-model="issuesOnly" type="checkbox"> 仅有问题</label>
        <div class="toolbar-spacer"></div>
        <UiSeg v-model="layout" :items="layoutOptions" aria-label="视图切换" />
      </div>

      <div v-if="selectedIds.length" class="bulk-bar">
        <span>已选 {{ selectedIds.length }} 份文档</span>
        <div class="bulk-actions">
          <UiButton size="sm" @click="pickerOpen = true">移入分区…</UiButton>
          <UiButton size="sm" title="只读服务不能重索引；点击复制 CLI 命令" @click="copyReindexCommands">复制重索引命令</UiButton>
          <UiButton variant="danger" size="sm" @click="removeDocuments(selectedIds)">删除所选</UiButton>
          <UiButton size="sm" @click="clearSelection">取消</UiButton>
        </div>
      </div>

      <DocTable
        v-if="layout === 'table'"
        :docs="filteredDocs"
        :selected-ids="selectedIds"
        deletable
        @open="doc => router.push(`/doc/${doc.id}`)"
        @toggle-select="toggleSelect"
        @delete="removeDocument"
      />

      <div v-else class="doc-grid">
        <article
          v-for="doc in filteredDocs"
          :key="doc.id"
          class="doc-card"
          @click="router.push(`/doc/${doc.id}`)"
        >
          <div class="doc-card-top">
            <div class="file-icon" :class="fileClass(doc)">{{ fileExt(doc) }}</div>
            <div>
              <div class="doc-card-title">{{ doc.title }}</div>
              <div class="doc-card-sub">{{ doc.filename }}</div>
              <IngestNote :doc="doc" />
            </div>
          </div>
          <div class="doc-card-meta">
            <span>{{ doc.pages }} 页</span>
            <span>{{ doc.chunks }} 切片</span>
            <UiChip v-if="doc.collectionName" size="sm">{{ doc.collectionName }}</UiChip>
            <UiButton
              class="card-delete"
              variant="danger"
              size="sm"
              @click.stop="removeDocument(doc)"
            >删除</UiButton>
          </div>
          <div class="doc-card-foot">
            <span class="status" :class="doc.status"><span class="dot"></span>{{ statusLabel(doc.status) }}</span>
            <span class="faint">{{ coverageText(doc) }}</span>
          </div>
        </article>
      </div>

      <UiEmpty v-if="!listReady" title="加载中…" hint="正在读取知识库。" />
      <UiEmpty
        v-else-if="!filteredDocs.length && !documents.length"
        title="档案库还是空的"
        hint="上传 PDF / DOCX / 图片 / Markdown 开始。扫描件才需要 OCR Key，文本层 PDF 不需要。"
      >
        <UiButton
          variant="primary"
          size="sm"
          style="margin-top: 10px"
          @click="uploadOpen = true"
        >
          上传第一份文档
        </UiButton>
      </UiEmpty>
      <UiEmpty
        v-else-if="!filteredDocs.length"
        title="没有匹配的文档"
        hint="调整筛选条件或清空搜索。"
      />
    </div>

    <CollectionPanel
      :open="panelOpen"
      @close="panelOpen = false"
      @changed="onCollectionsChanged"
    />
    <CollectionPicker
      :open="pickerOpen"
      :count="selectedIds.length"
      @close="pickerOpen = false"
      @assign="assign"
    />
    <UploadDialog
      :open="uploadOpen"
      :initial-collection="uploadTarget"
      :initial="uploadPrefill"
      @close="closeUpload"
      @done="onUploadDone"
    />
  </section>
</template>

<style scoped>
.card-delete { margin-left: auto; }
</style>
