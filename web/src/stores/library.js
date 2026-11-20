import { computed, ref, shallowRef, watch } from 'vue'
import { defineStore } from 'pinia'
import { fetchDocuments, fetchOverview, searchDocuments } from '@/api'

export const useLibraryStore = defineStore('library', () => {
  const documents = shallowRef([])
  const overview = shallowRef(null)
  const loading = shallowRef(false)
  const loaded = shallowRef(false)

  // filters
  const query = shallowRef('')
  const status = shallowRef('all')
  const issuesOnly = shallowRef(false)
  const source = shallowRef('all') // all | ocr | text | llm
  const collection = shallowRef('all') // all | unassigned | <collection id>
  const layout = shallowRef('table')

  // Text matching runs on the server: title + keywords + TOC headings, with
  // jieba segmentation. Client-side includes() cannot rank, and documents in
  // a series can share one preamble listing every part — a literal match
  // would bury the document actually about the topic under its siblings.
  const matchedIds = ref(null) // null = no text query
  const matching = shallowRef(false)

  async function load() {
    if (loading.value) return
    loading.value = true
    try {
      const [docs, stats] = await Promise.all([fetchDocuments(), fetchOverview()])
      documents.value = docs.documents
      overview.value = stats
      loaded.value = true
    } finally {
      loading.value = false
    }
  }

  async function refresh() {
    loaded.value = false
    await load()
  }

  // Latest query wins: typing 「方法」 fires 「方」「方法」 and the slower
  // response must not overwrite the newer one.
  let seq = 0
  watch(query, async (value) => {
    const q = value.trim()
    const mine = ++seq
    if (!q) {
      matchedIds.value = null
      matching.value = false
      return
    }
    matching.value = true
    try {
      const data = await searchDocuments(q)
      if (mine === seq) matchedIds.value = data.ids
    } catch {
      if (mine === seq) matchedIds.value = []
    } finally {
      if (mine === seq) matching.value = false
    }
  })

  const filteredDocs = computed(() => {
    const ids = matchedIds.value
    const rank = ids === null ? null : new Map(ids.map((id, i) => [id, i]))
    const docs = documents.value.filter((doc) => {
      if (rank !== null && !rank.has(doc.id)) return false
      if (status.value !== 'all' && doc.status !== status.value) return false
      if (issuesOnly.value && !(doc.errors || doc.warnings || doc.status === 'failed')) return false
      if (collection.value !== 'all') {
        if (collection.value === 'unassigned') {
          if (doc.collectionId) return false
        } else if (doc.collectionId !== collection.value) return false
      }
      if (source.value !== 'all') {
        const p = doc.provenance || {}
        if (source.value === 'ocr' && !p.ocr_used) return false
        if (source.value === 'text' && p.ocr_used !== false) return false
        if (source.value === 'llm' && !p.llm_used) return false
      }
      return true
    })
    // Server relevance order survives the other filters; without a text
    // query the list keeps its load order (newest first).
    if (rank !== null) docs.sort((a, b) => rank.get(a.id) - rank.get(b.id))
    return docs
  })

  const issueDocs = computed(() =>
    documents.value.filter((doc) => doc.errors || doc.warnings || doc.status === 'failed'),
  )

  return {
    documents,
    overview,
    loading,
    loaded,
    matching,
    query,
    status,
    issuesOnly,
    source,
    collection,
    layout,
    load,
    refresh,
    filteredDocs,
    issueDocs,
  }
})
