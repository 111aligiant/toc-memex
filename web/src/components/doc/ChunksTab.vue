<script setup>
import { computed, nextTick, ref, watch } from 'vue'
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

const sections = ref({ content: true, resources: true, review: true, sliceNotes: true })
const previewResource = ref(null)

function toggle(key) {
  sections.value = { ...sections.value, [key]: !sections.value[key] }
}

const chunk = () => workspace.selectedChunk

const noteCounts = computed(() => {
  const map = new Map()
  for (const note of notesStore.docNotes) {
    if (note.chunkId != null) map.set(note.chunkId, (map.get(note.chunkId) || 0) + 1)
  }
  return map
})

function chunkNoteCount(c) {
  const own = noteCounts.value.get(c.id) || 0
  const viaNode = c.nodeId
    ? notesStore.docNotes.filter((note) => note.chunkId == null && note.nodeId === c.nodeId).length
    : 0
  return own + viaNode
}

function clearScope() {
  workspace.chunkScopeId = null
}

function reviewLabel(id) {
  const state = workspace.reviewFor(id)
  return { pass: '通过', issue: '需调整' }[state] || ''
}

function setNote(event) {
  const id = workspace.selectedChunkId
  if (id != null) workspace.setReview(id, workspace.reviewFor(id), event.target.value)
}

/** Notes anchored to the selected slice itself or to its TOC node. */
const chunkNotes = () =>
  workspace.selectedChunk
    ? notesStore.notesForChunk(workspace.selectedChunk.id, workspace.selectedChunk.nodeId)
    : []

// A jump from 目录 / 手记 lands here: bring the selected slice into the list view.
watch(
  () => workspace.selectedChunkId,
  async (id) => {
    if (id == null) return
    await nextTick()
    document.querySelector(`.chunk-row[data-chunk-id="${id}"]`)?.scrollIntoView({ block: 'nearest' })
  },
)

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

async function showPageText(chunk) {
  if (chunk.pageStart == null) {
    ui.notify('该切片没有页码信息')
    return
  }
  await workspace.loadPage(chunk.pageStart)
  ui.notify(workspace.pageText ? '页文本已加载到下方' : '页文本不可用（无 OCR 缓存）')
}

/** Open the original file at the selected chunk's page (1-based hash). */
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
</script>

<template>
  <div class="chunks-layout">
    <div class="chunk-list">
      <div class="chunk-list-head">
        <span>
          {{ workspace.scopedChunks.length }} / {{ workspace.stats?.chunks }} 个切片
          <template v-if="workspace.chunkScopeNode"> · {{ workspace.chunkScopeNode.title }}</template>
        </span>
        <UiButton variant="quiet" size="sm" v-if="workspace.chunkScopeId" @click="clearScope">看全部</UiButton>
        <span v-else>{{ workspace.selectedNode?.title || '全部' }}</span>
        <UiButton
          variant="quiet"
          size="sm"
          v-if="Number.isInteger(workspace.selectedChunk?.pageStart)"
          @click="openChunkPage(workspace.selectedChunk)"
        >
          原文第 {{ workspace.selectedChunk.pageStart + 1 }} 页
        </UiButton>
      </div>
      <div
        v-for="c in workspace.scopedChunks"
        :key="c.id"
        :data-chunk-id="c.id"
        class="chunk-row"
        :class="{ selected: c.id === workspace.selectedChunkId, 'has-notes': chunkNoteCount(c) }"
        @click="workspace.selectedChunkId = c.id"
      >
        <div class="chunk-top">
          <span class="chunk-index">#{{ c.index }}</span>
          <span class="chunk-title">{{ c.title }}</span>
          <button
            v-if="chunkNoteCount(c)"
            class="chunk-notes-toggle"
            :title="`${chunkNoteCount(c)} 条手记 · 打开手记`"
            @click.stop="emit('open-notes', { chunkId: c.id, nodeId: c.nodeId })"
          >✎ {{ chunkNoteCount(c) }}</button>
          <span v-if="c.resources.length" class="resource-count">{{ c.resources.length }} 图</span>
          <span class="chunk-status" :class="workspace.reviewFor(c.id)">{{ reviewLabel(c.id) }}</span>
        </div>
        <div class="chunk-excerpt">{{ c.excerpt }}</div>
        <div class="chunk-meta"><span>{{ c.pages }}</span><span>{{ c.chars }} 字</span><span>{{ c.tokens }} tok</span></div>
      </div>
    </div>

    <section v-if="chunk()" class="chunk-editor">
      <div class="editor-head">
        <div class="editor-head-main">
          <div class="editor-title">{{ chunk().title || '(无标题)' }}</div>
          <div class="editor-sub">chunk #{{ chunk().index }} · {{ chunk().nodeId || '无节点' }}</div>
        </div>
        <div class="detail-nav">
          <button title="上一个" @click="workspace.moveChunk(-1)">↑</button>
          <button title="下一个" @click="workspace.moveChunk(1)">↓</button>
        </div>
      </div>
      <div class="editor-meta">
        <span>{{ chunk().pages }}</span><i class="sep"></i>
        <span>{{ chunk().chars }} 字</span><i class="sep"></i>
        <span>{{ chunk().tokens }} tokens</span><i class="sep"></i>
        <span>L{{ chunk().level }}</span>
        <span v-if="chunk().resources.length" class="resource-badge">{{ chunk().resources.length }} 个关联资源</span>
      </div>

      <div class="editor-section">
        <button class="editor-section-head" @click="toggle('content')">
          <span class="label">切片内容</span>
          <span class="hint">{{ sections.content ? '收起' : '展开' }} <svg class="chevron" :class="{ open: sections.content }" viewBox="0 0 24 24"><path d="m9 6 6 6-6 6"/></svg></span>
        </button>
        <div v-show="sections.content" class="editor-body chunk-body-html" v-html="chunk().bodyHtml"></div>
      </div>

      <div v-if="chunk().resources.length" class="editor-section">
        <button class="editor-section-head" @click="toggle('resources')">
          <span class="label">关联图片 / 表格</span>
          <span class="hint">{{ sections.resources ? '收起' : '展开' }} <svg class="chevron" :class="{ open: sections.resources }" viewBox="0 0 24 24"><path d="m9 6 6 6-6 6"/></svg></span>
        </button>
        <div v-show="sections.resources" class="resource-strip">
          <button
            v-for="resource in chunk().resources"
            :key="resource.id"
            class="resource-thumb"
            @click="previewResource = resource"
          >
            <img
              v-if="resource.image"
              :src="resource.image"
              :alt="resource.title"
              loading="lazy"
            >
            <img v-else :src="`data:image/svg+xml;charset=utf-8,${encodeURIComponent('<svg xmlns=\'http://www.w3.org/2000/svg\' width=\'360\' height=\'200\'><rect width=\'360\' height=\'200\' fill=\'#f8fafc\'/><text x=\'20\' y=\'100\' font-family=\'sans-serif\' font-size=\'14\' fill=\'#9aa1ac\'>' + (resource.type === 'table' ? '表格 · 无预览' : '图片缺失') + '</text></svg>')}`" :alt="resource.title">
            <span class="resource-thumb-title">{{ resource.title || resource.id }}</span>
            <span class="resource-thumb-meta">{{ resource.pageLabel }} · {{ resource.meta }}</span>
          </button>
        </div>
      </div>

      <div class="editor-section">
        <button class="editor-section-head" @click="toggle('review')">
          <span class="label">复核</span>
          <span class="hint">{{ workspace.reviewFor(chunk().id) ? reviewLabel(chunk().id) : '未标注' }} <svg class="chevron" :class="{ open: sections.review }" viewBox="0 0 24 24"><path d="m9 6 6 6-6 6"/></svg></span>
        </button>
        <div v-show="sections.review">
          <div class="review-bar">
            <button class="review-btn pass" :class="{ active: workspace.reviewFor(chunk().id) === 'pass' }" @click="workspace.setReview(chunk().id, 'pass')">通过</button>
            <button class="review-btn issue" :class="{ active: workspace.reviewFor(chunk().id) === 'issue' }" @click="workspace.setReview(chunk().id, 'issue')">需调整</button>
            <button class="review-btn" @click="workspace.setReview(chunk().id, '')">清除</button>
          </div>
          <textarea
            class="review-note editor-note"
            placeholder="记录问题或修改建议…"
            :value="workspace.reviewNote(chunk().id)"
            @change="setNote"
          ></textarea>
          <div class="detail-actions">
            <UiButton variant="quiet" size="sm" @click="copyChunk(chunk())">复制内容</UiButton>
            <UiButton variant="quiet" size="sm" @click="showPageText(chunk())">查看页文本</UiButton>
            <UiButton variant="quiet" size="sm" v-if="chunk().nodeId" @click="workspace.tab = 'toc'">定位目录</UiButton>
          </div>
          <div v-if="workspace.pageText" class="detail-section">
            <div class="detail-section-head"><span class="label">页文本 P{{ workspace.pageText.page + 1 }}</span></div>
            <div class="detail-body">{{ workspace.pageText.text }}</div>
          </div>
        </div>
      </div>

      <div class="editor-section">
        <button class="editor-section-head" @click="toggle('sliceNotes')">
          <span class="label">本切片手记 · {{ chunkNotes().length }}</span>
          <span class="hint">{{ sections.sliceNotes ? '收起' : '展开' }} <svg class="chevron" :class="{ open: sections.sliceNotes }" viewBox="0 0 24 24"><path d="m9 6 6 6-6 6"/></svg></span>
        </button>
        <div v-show="sections.sliceNotes" class="slice-notes-wrap">
          <SliceNotes :chunk="chunk()" @open-notes="emit('open-notes', $event)" />
        </div>
      </div>
    </section>
    <div v-else class="chunk-empty"><UiEmpty title="没有切片" compact /></div>

    <ResourcePreview v-if="previewResource" :resource="previewResource" @close="previewResource = null" />
  </div>
</template>

<style scoped>
.slice-notes-wrap { margin-top: 4px; }
.chunk-empty { border-left: 1px solid var(--border); }
.chunk-row.has-notes {
  background: color-mix(in srgb, var(--warn) 8%, var(--surface));
  box-shadow: inset 2px 0 0 var(--warn);
}
.chunk-row.has-notes .chunk-title { color: var(--text); font-weight: 600; }
.chunk-row.has-notes.selected { background: var(--accent-soft); box-shadow: inset 2px 0 0 var(--accent); }
.chunk-notes-toggle {
  background: var(--warn-soft);
  border-radius: 3px;
  font-weight: 600;
}
</style>
