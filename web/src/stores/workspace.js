import { computed, shallowRef } from 'vue'
import { defineStore } from 'pinia'
import { fetchDocument, fetchPage } from '@/api'
import { chunkBody, pageRange, renderChunkBody } from '@/utils/format'

const REVIEWS_KEY = 'kb-reviews'

function readReviews() {
  try {
    return JSON.parse(localStorage.getItem(REVIEWS_KEY) || '{}')
  } catch {
    return {}
  }
}

function writeReviews(reviews) {
  try {
    localStorage.setItem(REVIEWS_KEY, JSON.stringify(reviews))
  } catch { /* private mode */ }
}

/** Build a tree from flat toc_nodes rows (parent_id links). */
function buildTree(nodes) {
  const byId = new Map()
  const roots = []
  for (const node of nodes) {
    byId.set(node.node_id, { ...node, children: [] })
  }
  for (const node of byId.values()) {
    const parent = byId.get(node.parent_id)
    if (parent) parent.children.push(node)
    else roots.push(node)
  }
  return { byId, roots }
}

export const useWorkspaceStore = defineStore('workspace', () => {
  const docId = shallowRef(null)
  const document = shallowRef(null)
  const nodes = shallowRef([])
  const chunks = shallowRef([])
  const coverage = shallowRef([])
  const stats = shallowRef(null)
  const provenance = shallowRef(null)
  // What /api/documents/{id}/export would contain: 'ocr' | 'text-layer' | null.
  const exportKind = shallowRef(null)
  const loading = shallowRef(false)
  const error = shallowRef('')

  const tab = shallowRef('read')
  const selectedNodeId = shallowRef(null)
  const selectedChunkId = shallowRef(null)
  const expanded = shallowRef([])
  const reviews = shallowRef(readReviews())
  const pageText = shallowRef(null)
  // When set, the 切片 tab shows only this node's subtree chunks.
  const chunkScopeId = shallowRef(null)
  // Chunk the read stream should scroll to once it renders (set by jumps from 目录 / 手记).
  const scrollTarget = shallowRef(null)

  let loadToken = 0

  function shapeChunk(chunk, levelByNode) {
    return {
      id: chunk.id,
      index: chunk.chunk_index,
      nodeId: chunk.node_id,
      title: chunk.heading_path || '',
      pages: pageRange(chunk.page_start, chunk.page_end),
      pageStart: chunk.page_start,
      pageEnd: chunk.page_end,
      chars: (chunk.content || '').length,
      tokens: chunk.token_count ?? 0,
      level: levelByNode.get(chunk.node_id) ?? 1,
      excerpt: (chunk.content || '').replace(/\s+/g, ' ').slice(0, 140),
      content: chunk.content || '',
      body: chunkBody({ content: chunk.content, title: chunk.heading_path || '' }),
      bodyHtml: renderChunkBody({ content: chunk.content, title: chunk.heading_path || '' }),
      resources: (chunk.resources || []).map((resource) => ({
        ...resource,
        pageLabel: resource.page != null ? `P${resource.page + 1}` : '',
        meta: resource.type === 'table' ? '表格' : '图',
      })),
    }
  }

  async function load(id) {
    const token = ++loadToken
    loading.value = true
    error.value = ''
    pageText.value = null
    // Clear before the fetch so the header never shows the previous
    // document's export kind while the new one loads.
    exportKind.value = null
    try {
      const payload = await fetchDocument(id)
      if (token !== loadToken) return
      const doc = payload.document
      docId.value = doc.id
      document.value = doc
      coverage.value = payload.coverage
      stats.value = payload.stats
      provenance.value = payload.provenance || null
      exportKind.value = payload.exportKind ?? null

      const tree = buildTree(payload.nodes)
      const levelByNode = new Map(payload.nodes.map((node) => [node.node_id, node.level]))
      for (const node of tree.byId.values()) {
        node.pageLabel = pageRange(node.page_start, node.page_end)
        node.chunkCount = node.subtreeChunks
        node.issue = node.page_start == null && node.directChunks === 0 && node.issues.length
          ? 'danger'
          : node.issues.length ? 'warn' : ''
        node.path = node.heading_path
      }
      nodes.value = payload.nodes.map((node) => tree.byId.get(node.node_id))
      chunks.value = payload.chunks.map((chunk) => shapeChunk(chunk, levelByNode))

      // restore/initialize selection
      expanded.value = tree.roots.slice(0, 3).map((node) => node.node_id)
      selectedNodeId.value = tree.roots[0]?.node_id ?? null
      selectedChunkId.value = chunks.value[0]?.id ?? null
      chunkScopeId.value = null
      scrollTarget.value = null
    } catch (err) {
      if (token === loadToken) {
        // leave no stale document behind when the requested id is invalid
        docId.value = null
        document.value = null
        nodes.value = []
        chunks.value = []
        coverage.value = []
        stats.value = null
        provenance.value = null
        exportKind.value = null
        selectedNodeId.value = null
        selectedChunkId.value = null
        error.value = err.message
      }
    } finally {
      if (token === loadToken) loading.value = false
    }
  }

  function reset() {
    loadToken += 1
    docId.value = null
    document.value = null
    nodes.value = []
    chunks.value = []
    coverage.value = []
    stats.value = null
    provenance.value = null
    exportKind.value = null
    error.value = ''
    pageText.value = null
    tab.value = 'read'
    selectedNodeId.value = null
    selectedChunkId.value = null
    expanded.value = []
    chunkScopeId.value = null
    scrollTarget.value = null
  }

  const treeRoots = computed(() => {
    const ids = new Set(nodes.value.map((node) => node.node_id))
    return nodes.value.filter((node) => !node.parent_id || !ids.has(node.parent_id))
  })

  const selectedNode = computed(() =>
    nodes.value.find((node) => node.node_id === selectedNodeId.value) || null,
  )

  const selectedChunk = computed(() =>
    chunks.value.find((chunk) => chunk.id === selectedChunkId.value) || null,
  )

  function nodeSubtreeIds(nodeId) {
    const byId = new Map(nodes.value.map((node) => [node.node_id, node]))
    const ids = []
    const walk = (id) => {
      ids.push(id)
      for (const node of nodes.value) {
        if (node.parent_id === id) walk(node.node_id)
      }
    }
    walk(nodeId)
    return ids
  }

  const nodeChunks = computed(() => {
    const node = selectedNode.value
    if (!node) return chunks.value
    const ids = new Set(nodeSubtreeIds(node.node_id))
    return chunks.value.filter((chunk) => chunk.nodeId && ids.has(chunk.nodeId))
  })

  /** Chunks listed in the 切片 tab: scoped when opened from 目录, all otherwise. */
  const scopedChunks = computed(() => {
    if (!chunkScopeId.value) return chunks.value
    const ids = new Set(nodeSubtreeIds(chunkScopeId.value))
    return chunks.value.filter((chunk) => chunk.nodeId && ids.has(chunk.nodeId))
  })

  const chunkScopeNode = computed(() =>
    chunkScopeId.value
      ? nodes.value.find((node) => node.node_id === chunkScopeId.value) || null
      : null,
  )

  function selectNode(node) {
    selectedNodeId.value = node.node_id
    const ids = new Set(nodeSubtreeIds(node.node_id))
    const first = chunks.value.find((chunk) => chunk.nodeId && ids.has(chunk.nodeId))
    if (!first) return false
    selectedChunkId.value = first.id
    return true
  }

  function selectNodeAndRead(node) {
    tab.value = 'read'
    const ok = selectNode(node)
    if (ok) scrollTarget.value = selectedChunkId.value
    return ok
  }

  /** 目录 tab → 切片 tab: scope the slice list to this node's subtree. */
  function openChunks(node) {
    chunkScopeId.value = node.node_id
    selectNode(node)
    tab.value = 'chunks'
  }

  /** Jump from a 手记 anchor to its slice (chunk first, then its node's first slice). */
  function openChunk(anchor = {}) {
    const chunk = anchor.chunkId != null
      ? chunks.value.find((item) => item.id === anchor.chunkId)
      : null
    if (chunk) {
      chunkScopeId.value = chunk.nodeId || null
      selectedChunkId.value = chunk.id
      if (chunk.nodeId) selectedNodeId.value = chunk.nodeId
    } else if (anchor.nodeId) {
      const node = nodes.value.find((item) => item.node_id === anchor.nodeId)
      if (node) {
        chunkScopeId.value = node.node_id
        selectNode(node)
      }
    }
    tab.value = 'chunks'
  }

  function toggleExpanded(id) {
    expanded.value = expanded.value.includes(id)
      ? expanded.value.filter((item) => item !== id)
      : [...expanded.value, id]
  }

  function moveChunk(delta) {
    const index = chunks.value.findIndex((chunk) => chunk.id === selectedChunkId.value)
    const next = chunks.value[index + delta]
    if (next) selectedChunkId.value = next.id
  }

  function reviewKey(chunkId) {
    return `${docId.value}:${chunkId}`
  }

  function reviewFor(chunkId) {
    return reviews.value[reviewKey(chunkId)]?.state || ''
  }

  function reviewNote(chunkId) {
    return reviews.value[reviewKey(chunkId)]?.note || ''
  }

  function setReview(chunkId, state, note) {
    const key = reviewKey(chunkId)
    const next = { ...reviews.value }
    if (!state && !note) delete next[key]
    else next[key] = { state: state || next[key]?.state || '', note: note ?? next[key]?.note ?? '' }
    reviews.value = next
    writeReviews(next)
  }

  async function loadPage(page) {
    if (!docId.value || page == null) return
    pageText.value = null
    try {
      pageText.value = await fetchPage(docId.value, page)
    } catch {
      pageText.value = null
    }
  }

  // Quality's "待复核" means still needs work, so passing marks do not count.
  const reviewCount = computed(() =>
    Object.values(reviews.value).filter((item) => item.state === 'issue').length,
  )

  return {
    docId,
    document,
    nodes,
    chunks,
    coverage,
    stats,
    provenance,
    exportKind,
    loading,
    error,
    tab,
    selectedNodeId,
    selectedChunkId,
    expanded,
    pageText,
    chunkScopeId,
    scrollTarget,
    treeRoots,
    selectedNode,
    selectedChunk,
    nodeChunks,
    scopedChunks,
    chunkScopeNode,
    reviewCount,
    load,
    reset,
    selectNode,
    selectNodeAndRead,
    openChunks,
    openChunk,
    toggleExpanded,
    moveChunk,
    reviewFor,
    reviewNote,
    setReview,
    loadPage,
  }
})
