<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import TocTree from '@/components/TocTree.vue'
import ResourceThumb from '@/components/ResourceThumb.vue'
import ResourcePreview from '@/components/doc/ResourcePreview.vue'
import SliceNotes from '@/components/doc/SliceNotes.vue'
import { useWorkspaceStore } from '@/stores/workspace'
import { useNotesStore } from '@/stores/notes'
import { useUiStore } from '@/stores/ui'
import UiEmpty from '@/components/ui/UiEmpty.vue'
import UiButton from '@/components/ui/UiButton.vue'
import { originalFileUrl } from '@/api'
import { copyText } from '@/utils/clipboard'

const workspace = useWorkspaceStore()
const notesStore = useNotesStore()
const ui = useUiStore()

const emit = defineEmits(['open-notes'])

const detailOpen = ref(true)

// ── ✎ markers: note counts per chunk, all detail lives in the 手记 tab ──
const titleNotes = computed(() =>
  notesStore.docNotes.filter((note) => !note.nodeId && !note.chunkId),
)
const chunkNoteCount = computed(() => {
  const map = new Map()
  for (const note of notesStore.docNotes) {
    if (note.chunkId != null) map.set(note.chunkId, (map.get(note.chunkId) || 0) + 1)
  }
  return map
})
const nodeNoteCount = computed(() => {
  const map = new Map()
  for (const note of notesStore.docNotes) {
    if (note.nodeId && note.chunkId == null) map.set(note.nodeId, (map.get(note.nodeId) || 0) + 1)
  }
  return map
})

function chunkNotes(chunk) {
  const own = chunkNoteCount.value.get(chunk.id) || 0
  const viaNode = chunk.nodeId ? nodeNoteCount.value.get(chunk.nodeId) || 0 : 0
  return own + viaNode
}

function openNotes(anchor) {
  emit('open-notes', anchor)
}

const streamChunks = computed(() => workspace.chunks)
const streamTitle = computed(() => {
  const node = workspace.selectedNode
  return node ? node.title : '全部切片'
})

const previewResource = ref(null)

function selectNode(node) {
  if (!workspace.selectNodeAndRead(node)) ui.notify('该节点暂无切片')
}

// 目录 / 手记 set scrollTarget; the stream only exists after the tab renders,
// so scroll once it is in the DOM. (The store can't do this: its `document`
// ref shadows the global.)
watch(
  () => workspace.scrollTarget,
  async (chunkId) => {
    if (chunkId == null) return
    await nextTick()
    document.querySelector(`[data-chunk-id="${chunkId}"]`)?.scrollIntoView({ block: 'start', behavior: 'smooth' })
    workspace.scrollTarget = null
  },
  { immediate: true },
)

function reviewLabel(chunk) {
  const state = workspace.reviewFor(chunk.id)
  return { pass: '通过', issue: '需调整' }[state] || ''
}

/** 复制切片正文。只有真的写进剪贴板才报成功。 */
async function copyChunk(chunk) {
  const text = chunk.body || chunk.content || ''
  if (!text.trim()) {
    ui.notify('这条切片没有可复制的内容')
    return
  }
  const ok = await copyText(text)
  ui.notify(ok ? '已复制切片内容' : '复制失败：浏览器拒绝了剪贴板访问')
}

/** Open the original file at this chunk's page (URL hash is 1-based). */
function openChunkPage(chunk) {
  const provenance = workspace.provenance?.current
  if (provenance && !provenance.file.exists) {
    ui.notify('原始文件已不在原路径，无法打开')
    return
  }
  const page = Number(chunk?.pageStart)
  if (!Number.isInteger(page) || page < 0 || !workspace.docId) return
  window.open(
    `${originalFileUrl(workspace.docId)}#page=${page + 1}`,
    '_blank',
    'noopener',
  )
}

async function showPageText(chunk) {
  const page = chunk.pageStart
  if (page == null) {
    ui.notify('该切片没有页码信息')
    return
  }
  await workspace.loadPage(page)
  if (workspace.pageText) {
    ui.notify('页文本已加载到底部详情')
  } else {
    ui.notify('页文本不可用（无 OCR 缓存）')
  }
}
</script>

<template>
  <div class="reader-layout" :class="{ 'detail-collapsed': !detailOpen }">
    <div class="tree-panel">
      <TocTree
        :nodes="workspace.nodes"
        :expanded="workspace.expanded"
        :selected-id="workspace.selectedNodeId"
        heading="目录"
        :hint="`${workspace.treeRoots.length} 顶层`"
        @select="selectNode"
        @toggle="workspace.toggleExpanded"
      />
    </div>

    <div class="chunk-stream">
      <div class="chunk-stream-head">
        <div>
          <h3>{{ streamTitle }}</h3>
          <div class="chunk-stream-sub">{{ streamChunks.length }} / {{ workspace.stats?.chunks }} 个切片 · 全文档</div>
        </div>
        <div class="stream-head-actions">
          <span class="faint">按切片显示正文</span>
          <UiButton variant="quiet" size="sm" class="detail-toggle" @click="detailOpen = !detailOpen">{{ detailOpen ? '收起详情' : '展开详情' }}</UiButton>
        </div>
      </div>

      <!-- 整份文档级的手记：只显示数量，内容都在手记 tab -->
      <div v-if="titleNotes.length" class="title-page">
        <button class="title-page-head" @click="openNotes({})">
          <span class="label">手记 · 整份文档 {{ titleNotes.length }}</span>
          <span class="hint">在手记 tab 查看</span>
        </button>
      </div>

      <div v-if="streamChunks.length" class="chunk-stream-body">
        <article
          v-for="chunk in streamChunks"
          :key="chunk.id"
          :data-chunk-id="chunk.id"
          class="chunk-block"
          :class="{ selected: chunk.id === workspace.selectedChunkId }"
          @click="workspace.selectedChunkId = chunk.id"
        >
          <div class="chunk-block-head">
            <span class="chunk-index">#{{ chunk.index }}</span>
            <span class="chunk-block-title">{{ chunk.title }}</span>
            <span class="chunk-block-page">{{ chunk.pages }}</span>
            <button
              v-if="chunkNotes(chunk)"
              class="chunk-notes-toggle"
              :title="`${chunkNotes(chunk)} 条手记 · 打开手记 tab`"
              @click.stop="openNotes({ chunkId: chunk.id, nodeId: chunk.nodeId })"
            >
              ✎ {{ chunkNotes(chunk) }}
            </button>
            <span v-if="chunk.resources.length" class="resource-count">{{ chunk.resources.length }} 图</span>
            <span v-if="reviewLabel(chunk)" class="chunk-status" :class="workspace.reviewFor(chunk.id)">{{ reviewLabel(chunk) }}</span>
          </div>
          <div class="chunk-block-body chunk-body-html" v-html="chunk.bodyHtml"></div>
          <div v-if="chunk.resources.length" class="chunk-block-resources">
            <ResourceThumb v-for="resource in chunk.resources" :key="resource.id" :resource="resource" @open="previewResource = $event" />
          </div>
          <div class="chunk-block-foot">
            <span>{{ chunk.chars }} 字</span>
            <span>{{ chunk.tokens }} tokens</span>
            <span>L{{ chunk.level }}</span>
          </div>
        </article>
      </div>
      <UiEmpty
        v-else
        title="该文档暂无切片"
        hint="请检查目录页码或重新索引。"
      />
    </div>

    <aside v-if="workspace.selectedChunk && detailOpen" class="detail-panel reader-detail">
      <div class="detail-head">
        <div class="detail-head-main">
          <div class="detail-title">{{ workspace.selectedChunk.title || '(无标题)' }}</div>
          <div class="detail-sub">chunk #{{ workspace.selectedChunk.index }} · {{ workspace.selectedChunk.nodeId || '无节点' }}</div>
        </div>
        <div class="detail-nav">
          <button title="上一个" @click="workspace.moveChunk(-1)">↑</button>
          <button title="下一个" @click="workspace.moveChunk(1)">↓</button>
        </div>
      </div>
      <div class="detail-meta">
        <span>{{ workspace.selectedChunk.pages }}</span><i class="sep"></i>
        <span>{{ workspace.selectedChunk.chars }} 字</span><i class="sep"></i>
        <span>{{ workspace.selectedChunk.tokens }} tokens</span><i class="sep"></i>
        <span>L{{ workspace.selectedChunk.level }}</span>
      </div>
      <div class="detail-section">
        <div class="detail-section-head"><span class="label">切片内容</span></div>
        <div class="detail-body chunk-body-box">{{ workspace.selectedChunk.body || workspace.selectedChunk.content }}</div>
      </div>
      <div class="detail-section">
        <div class="detail-section-head">
          <span class="label">复核</span>
          <span class="hint">{{ workspace.reviewFor(workspace.selectedChunk.id) ? (workspace.reviewFor(workspace.selectedChunk.id) === 'pass' ? '已通过' : '需调整') : '未标注' }}</span>
        </div>
        <div class="review-bar">
          <button class="review-btn pass" :class="{ active: workspace.reviewFor(workspace.selectedChunk.id) === 'pass' }" @click="workspace.setReview(workspace.selectedChunk.id, 'pass')">通过</button>
          <button class="review-btn issue" :class="{ active: workspace.reviewFor(workspace.selectedChunk.id) === 'issue' }" @click="workspace.setReview(workspace.selectedChunk.id, 'issue')">需调整</button>
          <button class="review-btn" @click="workspace.setReview(workspace.selectedChunk.id, '')">清除</button>
        </div>
        <textarea
          class="review-note"
          placeholder="记录问题或修改建议…"
          :value="workspace.reviewNote(workspace.selectedChunk.id)"
          @change="workspace.setReview(workspace.selectedChunk.id, workspace.reviewFor(workspace.selectedChunk.id), $event.target.value)"
        ></textarea>
        <div class="detail-actions">
          <UiButton variant="quiet" size="sm" @click="copyChunk(workspace.selectedChunk)">复制内容</UiButton>
          <UiButton
            v-if="Number.isInteger(workspace.selectedChunk.pageStart)"
            variant="quiet"
            size="sm"
            @click="openChunkPage(workspace.selectedChunk)"
          >
            原文第 {{ workspace.selectedChunk.pageStart + 1 }} 页
          </UiButton>
          <UiButton variant="quiet" size="sm" @click="showPageText(workspace.selectedChunk)">查看页文本</UiButton>
          <UiButton variant="quiet" size="sm" v-if="workspace.selectedChunk.nodeId" @click="workspace.tab = 'toc'">定位目录</UiButton>
        </div>
        <div v-if="workspace.pageText" class="page-text-box">
          <div class="detail-section-head mt-10"><span class="label">页文本 P{{ workspace.pageText.page + 1 }}</span></div>
          <div class="detail-body">{{ workspace.pageText.text }}</div>
        </div>
      </div>
      <div class="detail-section">
        <div class="detail-section-head">
          <span class="label">本切片手记 · {{ chunkNotes(workspace.selectedChunk) }}</span>
          <span class="hint">可直接在下方写</span>
        </div>
        <SliceNotes :chunk="workspace.selectedChunk" compact @open-notes="openNotes" />
      </div>
    </aside>

    <ResourcePreview v-if="previewResource" :resource="previewResource" @close="previewResource = null" />
  </div>
</template>

<style scoped>
.stream-head-actions { display: flex; align-items: center; gap: 10px; }
.page-text-box { min-width: 0; }
.chunk-body-box { max-height: 300px; }
/* Keep the sticky panel inside the viewport so 复核 / 手记 stay reachable. */
.reader-detail { max-height: calc(100vh - 92px); overflow: auto; }
</style>
