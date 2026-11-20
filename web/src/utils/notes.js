/** Shared rendering / grouping helpers for agent notes. */

import DOMPurify from 'dompurify'

export function escapeHtml(text) {
  return (text || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

/**
 * Escape note text, then highlight @mentions. Safe for v-html.
 *
 * Stored note bodies can contain live HTML: the ingest pipeline decodes
 * HTML entities (html.unescape) from source documents, so '&#x27;'-style
 * payloads may already be raw markup in the DB. escapeHtml alone only
 * covers & < > quotes, so DOMPurify sanitizes the final markup as the
 * defense-in-depth net over every caller.
 */
export function renderNoteBody(text) {
  const escaped = escapeHtml(text)
  const mentioned = escaped.replace(
    /@([A-Za-z0-9_.-]+)/g,
    '<span class="mention">@$1</span>'
  )
  return DOMPurify.sanitize(mentioned, { USE_PROFILES: { html: true } })
}

/** Group notes into root notes and their replies (one level deep). */
export function groupReplies(notes) {
  // A reply whose parent was filtered out (other kind, other anchor group)
  // would otherwise render nowhere — show it as a root instead.
  const ids = new Set(notes.map((note) => note.id))
  const roots = notes.filter((note) => note.replyTo == null || !ids.has(note.replyTo))
  const repliesByParent = new Map()
  for (const note of notes) {
    if (note.replyTo != null) {
      if (!repliesByParent.has(note.replyTo)) repliesByParent.set(note.replyTo, [])
      repliesByParent.get(note.replyTo).push(note)
    }
  }
  return { roots, repliesByParent }
}

// 作者徽标一律配白字，所以调色板必须是深色系、且不跟随主题：
// 用 var(--accent) 的话，暗色主题下会变成浅蓝 #6ea0ff，白字读不清。
const AUTHOR_PALETTE = ['#2563eb', '#0d9488', '#8b5cf6', '#b45309', '#be185d']

/** Deterministic badge color per author name. */
export function authorColor(name) {
  let hash = 0
  for (const char of String(name)) hash = (hash * 31 + char.charCodeAt(0)) >>> 0
  return AUTHOR_PALETTE[hash % AUTHOR_PALETTE.length]
}
