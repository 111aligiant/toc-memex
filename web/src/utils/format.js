/** Presentation helpers shared across views. */

export function statusLabel(status) {
  return {
    ready: '就绪',
    parsing: '解析中',
    indexing: '解析中',
    failed: '失败',
    pending: '排队中',
  }[status] || status
}

export function fileExt(doc) {
  return { pdf: 'PDF', doc: 'DOC', md: 'MD', xlsx: 'XLS' }[doc.type] || 'DOC'
}

export function fileClass(doc) {
  return doc.type === 'pdf' ? 'pdf' : 'doc'
}

export function formatBytes(size) {
  if (size == null) return '—'
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / 1024 / 1024).toFixed(1)} MB`
}

export function formatTokens(count) {
  const value = Number(count) || 0
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`
  if (value >= 100_000) return `${Math.round(value / 1000)}k`
  if (value >= 1000) return `${(value / 1000).toFixed(1)}k`
  return String(value)
}

export function relTime(iso) {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  const seconds = Math.max(0, (Date.now() - date.getTime()) / 1000)
  if (seconds < 60) return '刚刚'
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`
  const pad = (value) => String(value).padStart(2, '0')
  const hm = `${pad(date.getHours())}:${pad(date.getMinutes())}`
  if (date.toDateString() === new Date().toDateString()) return hm
  const md = `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${hm}`
  if (date.getFullYear() === new Date().getFullYear()) return md
  return `${date.getFullYear()}-${md}`
}

export function absTime(iso) {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleString('zh-CN', { hour12: false })
}

export function coveragePercent(doc) {
  if (!doc.contentPages) return 0
  return Math.round((doc.covered / doc.contentPages) * 100)
}

export function coverageText(doc) {
  return doc.contentPages ? `${doc.covered}/${doc.contentPages}` : '—'
}

export function coverageTone(doc) {
  if (!doc.contentPages) return ''
  const missing = doc.contentPages - doc.covered
  if (doc.errors || missing > Math.max(2, doc.contentPages * 0.1)) return 'danger'
  if (missing) return 'warn'
  return ''
}

export function pageRange(start, end) {
  if (start == null) return '?'
  if (end == null || end === start) return `P${start + 1}`
  return `P${start + 1}–${end + 1}`
}

export function chunkBody(chunk) {
  if (!chunk) return ''
  const lines = (chunk.content || '').split('\n')
  const first = (lines[0] || '').trim()
  const title = (chunk.title || '').trim()
  if (first && first.length >= 2 && (first === title || title.includes(first))) {
    return lines.slice(1).join('\n').trim()
  }
  return chunk.content
}

/**
 * Make an FTS snippet safe for v-html: only our own <mark> highlight
 * survives. DOMPurify is the final net — snippet text comes from stored
 * chunk content which may contain raw HTML (the ingest pipeline decodes
 * HTML entities from source documents).
 */
import DOMPurify from 'dompurify'

function escapeHtml(value) {
  return String(value || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

function tableCells(line) {
  const inner = line.trim().replace(/^\|/, '').replace(/\|$/, '')
  const cells = []
  let buffer = ''
  for (let index = 0; index < inner.length; index += 1) {
    const char = inner[index]
    if (char === '\\' && inner[index + 1] === '|') {
      buffer += '|'
      index += 1
      continue
    }
    if (char === '|') {
      cells.push(buffer.trim())
      buffer = ''
      continue
    }
    buffer += char
  }
  cells.push(buffer.trim())
  return cells
}

const isTableLine = (line) => /^\s*\|.*\|\s*$/.test(line)
const isTableSeparator = (line) =>
  tableCells(line).every((cell) => /^:?-{3,}:?$/.test(cell.replace(/\s/g, '')))

/**
 * The chunk text sent to BM25 and the LLM stores tables as Markdown with
 * merged cells expanded. Render those tables as real tables in the reading
 * view instead of showing a wall of pipe characters; all text is escaped
 * before the table tags are inserted, then DOMPurify runs over the result.
 */
export function renderChunkBody(chunk) {
  const text = chunkBody(chunk)
  if (!text) return ''
  const lines = text.split('\n')
  const html = []
  let index = 0
  while (index < lines.length) {
    const isTableStart =
      isTableLine(lines[index]) &&
      index + 1 < lines.length &&
      isTableLine(lines[index + 1]) &&
      isTableSeparator(lines[index + 1])
    if (isTableStart) {
      const header = tableCells(lines[index])
      index += 2
      const rows = []
      while (index < lines.length && isTableLine(lines[index])) {
        rows.push(tableCells(lines[index]))
        index += 1
      }
      const headCells = []
      for (const cell of header) {
        const last = headCells[headCells.length - 1]
        if (last && last.text === cell) last.span += 1
        else headCells.push({ text: cell, span: 1 })
      }
      const head = `<thead><tr>${headCells
        .map((cell) => `<th${cell.span > 1 ? ` colspan="${cell.span}"` : ''}>${escapeHtml(cell.text)}</th>`)
        .join('')}</tr></thead>`
      const body = rows.length
        ? `<tbody>${rows.map((row) => `<tr>${row.map((cell) => `<td>${escapeHtml(cell)}</td>`).join('')}</tr>`).join('')}</tbody>`
        : ''
      html.push(`<div class="md-table-wrap"><table class="md-table">${head}${body}</table></div>`)
      continue
    }
    const parts = []
    while (index < lines.length) {
      const nextIsTable =
        isTableLine(lines[index]) &&
        index + 1 < lines.length &&
        isTableLine(lines[index + 1]) &&
        isTableSeparator(lines[index + 1])
      if (nextIsTable) break
      parts.push(lines[index])
      index += 1
    }
    html.push(`<div class="md-text">${parts.map(escapeHtml).join('<br>')}</div>`)
  }
  return DOMPurify.sanitize(html.join(''), { USE_PROFILES: { html: true } })
}

export function safeSnippet(snippet) {
  if (!snippet) return ''
  const marked = snippet
    .replace(/<b>/g, '\u0001')
    .replace(/<\/b>/g, '\u0002')
  const escaped = marked
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
  const restored = escaped
    .replace(/\u0001/g, '<mark>')
    .replace(/\u0002/g, '</mark>')
  return DOMPurify.sanitize(restored, { USE_PROFILES: { html: true } })
}
