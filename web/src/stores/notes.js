import { computed, shallowRef } from 'vue'
import { defineStore } from 'pinia'
import { addNote, deleteNote, fetchNotes, saveIngestNote } from '@/api'

const AUTHOR_KEY = 'kb-agent-name'
const SESSION_KEY = 'kb-agent-session'

function readAuthor() {
  try {
    return localStorage.getItem(AUTHOR_KEY) || ''
  } catch {
    return ''
  }
}

function writeAuthor(value) {
  try {
    localStorage.setItem(AUTHOR_KEY, value)
  } catch { /* private mode */ }
}

/**
 * One id per browser tab session, so notes written in one sitting group
 * together. Minted client-side: it groups, it does not prove identity —
 * the MCP path assigns its own id server-side instead.
 */
function readSessionId() {
  try {
    const existing = sessionStorage.getItem(SESSION_KEY)
    if (existing) return existing
    const minted = crypto.randomUUID().replace(/-/g, '')
    sessionStorage.setItem(SESSION_KEY, minted)
    return minted
  } catch {
    return null
  }
}

export const KIND_LABELS = {
  note: '笔记',
  pitfall: '踩坑',
  handoff: '交接',
  ingest: '入库备注',
}

export const useNotesStore = defineStore('notes', () => {
  const notes = shallowRef([])
  const loading = shallowRef(false)
  const author = shallowRef(readAuthor())
  const sessionId = shallowRef(readSessionId())
  const docFilter = shallowRef('')

  // notes anchored to the document open in the workspace
  const docNotes = shallowRef([])
  const docNotesDocId = shallowRef(null)
  const replyTo = shallowRef(null) // note id being answered
  // Anchor the 手记 tab should scroll to after a ✎ marker was clicked:
  // { chunkId } | { nodeId } | {} for the whole document.
  const anchorFocus = shallowRef(null)

  async function load() {
    loading.value = true
    try {
      const data = await fetchNotes({
        ...(docFilter.value ? { docId: docFilter.value } : {}),
        // The 手记 page filters kinds client-side; 50 rows used to leave the
        // 入库备注 bucket empty on a busy library even though notes existed.
        limit: 200,
      })
      notes.value = data.notes
    } finally {
      loading.value = false
    }
  }

  async function loadDoc(docId) {
    if (!docId) {
      docNotes.value = []
      docNotesDocId.value = null
      return
    }
    const data = await fetchNotes({ docId })
    docNotes.value = data.notes
    docNotesDocId.value = docId
  }

  async function post({ kind, body, docId = null, nodeId = null, chunkId = null, issueId = null, replyToId = null }) {
    const note = await addNote({
      author: author.value || 'anonymous',
      kind,
      body,
      doc_id: docId,
      node_id: nodeId,
      chunk_id: chunkId,
      issue_id: issueId,
      session_id: sessionId.value,
      reply_to: replyToId,
    })
    replyTo.value = null
    if (docNotesDocId.value && docId === docNotesDocId.value) await loadDoc(docId)
    await load()
    return note
  }

  function startReply(note) {
    replyTo.value = note.id
    return `@${note.author} `
  }

  /** Delete an agent note (and its replies); the human cleanup path. */
  async function remove(note) {
    await deleteNote(note.id)
    if (docNotesDocId.value && note.docId === docNotesDocId.value) await loadDoc(note.docId)
    await load()
  }

  /** Set (or clear, with an empty string) a document's human-owned 入库备注. */
  async function saveIngest(docId, body) {
    await saveIngestNote(docId, body)
    await load()
  }

  function cancelReply() {
    replyTo.value = null
  }

  function setAuthor(value) {
    author.value = value.trim()
    writeAuthor(author.value)
  }

  /** Notes attached to a chunk, including the ones attached to its node. */
  function notesForChunk(chunkId, nodeId = null) {
    return docNotes.value.filter((note) =>
      note.chunkId === chunkId || (note.chunkId == null && nodeId && note.nodeId === nodeId),
    )
  }

  function notesForNode(nodeId) {
    return docNotes.value.filter((note) => note.nodeId === nodeId && note.chunkId == null)
  }

  /** Ask the 手记 tab to open on a given anchor (chunk / node / whole doc). */
  function focusAnchor(anchor) {
    anchorFocus.value = anchor
  }

  const byKind = computed(() => {
    const out = { note: [], pitfall: [], handoff: [], ingest: [] }
    for (const item of notes.value) out[item.kind]?.push(item)
    return out
  })

  return {
    notes,
    loading,
    author,
    sessionId,
    docFilter,
    docNotes,
    docNotesDocId,
    replyTo,
    anchorFocus,
    load,
    loadDoc,
    post,
    startReply,
    cancelReply,
    remove,
    saveIngest,
    setAuthor,
    notesForChunk,
    notesForNode,
    focusAnchor,
    byKind,
  }
})
