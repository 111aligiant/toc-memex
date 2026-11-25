/** Toc Memex knowledge-base API client. Shapes mirror app/web_api.py. */

import { downloadBlob } from '@/utils/download'

/**
 * Parse a response body as JSON, tolerating a non-JSON body.
 *
 * A stale server (one started before a route existed) answers an unknown
 * /api path with the SPA's index.html. `response.json()` then throws
 * "Unexpected token '<'", which reads as a frontend bug when the real
 * problem is "restart the server". This turns that into a plain message.
 */
async function readJson(response) {
  const text = await response.text()
  try {
    return JSON.parse(text)
  } catch {
    if (/^\s*<(!doctype|html)/i.test(text)) {
      throw new Error(
        '服务端没有这个接口，返回的是网页。多半是服务进程还是旧版本——重启 python -m app.web_api 后再试。',
      )
    }
    throw new Error(`服务端返回的不是 JSON（${response.status}）`)
  }
}

async function request(path, options = {}) {
  const response = await fetch(path, options)
  if (!response.ok) {
    const detail = await readJson(response).catch(() => ({}))
    throw new Error(detail.detail || `${response.status} ${response.statusText}`)
  }
  return readJson(response)
}

export function fetchOverview() {
  return request('/api/overview')
}

export function fetchDocuments() {
  return request('/api/documents')
}

/** Document ids matching `q`, most relevant first (summary/keywords via doc_fts). */
export function searchDocuments(q) {
  return request(`/api/documents/search?q=${encodeURIComponent(q)}`)
}

export function fetchDocument(id) {
  return request(`/api/documents/${encodeURIComponent(id)}`)
}

export function fetchPage(id, page) {
  return request(`/api/documents/${encodeURIComponent(id)}/page/${page}`)
}

/** Inline URL of the original file (PDF/DOCX/…), for the 打开原文 button. */
export function originalFileUrl(id) {
  return `/api/documents/${encodeURIComponent(id)}/file`
}

/** The original bytes, used to pre-fill a retry upload with the same file. */
export async function fetchOriginalBlob(id) {
  const response = await fetch(originalFileUrl(id))
  if (!response.ok) {
    const detail = await readJson(response).catch(() => ({}))
    throw new Error(detail.detail || `${response.status} ${response.statusText}`)
  }
  return response.blob()
}

/**
 * Download the parse output a document was built from, as a ZIP.
 * Returns the blob, the server-chosen filename and the export kind
 * ('ocr' | 'text-layer') so the caller can say what was actually exported.
 */
export async function exportDocument(id) {
  const response = await fetch(`/api/documents/${encodeURIComponent(id)}/export`)
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}))
    throw new Error(detail.detail || `${response.status} ${response.statusText}`)
  }
  const blob = await response.blob()
  const disposition = response.headers.get('Content-Disposition') || ''
  const match = /filename\*=UTF-8''([^;]+)/i.exec(disposition)
  return {
    blob,
    filename: match ? decodeURIComponent(match[1]) : `document-${id}.zip`,
    kind: response.headers.get('X-Export-Kind') || '',
  }
}

export function fetchSettings() {
  return request('/api/settings')
}

export function fetchJobs() {
  return request('/api/jobs')
}

export function fetchTasks() {
  return request('/api/tasks')
}

export function fetchDocumentChunks(docId) {
  return request(`/api/documents/${encodeURIComponent(docId)}/chunks`)
}

export function fetchQuality() {
  return request('/api/quality')
}

/** Baselines for one document (ADR-0002 §4): pre-curator derived state. */
export function fetchBaselines(docId) {
  return request(`/api/documents/${encodeURIComponent(docId)}/baselines`)
}

/** Restore a document to a baseline. Discards every curator change to it. */
export function restoreBaseline(docId, { baselineId = null, force = false } = {}) {
  return request(`/api/documents/${encodeURIComponent(docId)}/restore-baseline`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ baseline_id: baselineId, confirm: true, force }),
  })
}

/** Maintenance proposals awaiting human approval (ADR-0002 §13 P3). */
export function fetchCuration(verdict = 'pending') {
  const params = verdict ? `?verdict=${encodeURIComponent(verdict)}` : ''
  return request(`/api/curation${params}`)
}

export function approveProposal(proposalId) {
  return request(`/api/curation/${encodeURIComponent(proposalId)}/approve`, { method: 'POST' })
}

export function rejectProposal(proposalId) {
  return request(`/api/curation/${encodeURIComponent(proposalId)}/reject`, { method: 'POST' })
}

/** LLM call log and token aggregates (ADR-0002 §10.4). */
export function fetchLlmUsage() {
  return request('/api/llm/usage')
}
export function fetchLlmCalls({ docId = '', runId = '', callSite = '', status = '', limit = 100, offset = 0 } = {}) {
  const params = new URLSearchParams()
  if (docId) params.set('doc_id', docId)
  if (runId) params.set('run_id', runId)
  if (callSite) params.set('call_site', callSite)
  if (status) params.set('status', status)
  params.set('limit', String(limit))
  params.set('offset', String(offset))
  return request(`/api/llm/calls?${params}`)
}

/**
 * Download the whole call log (same filters, uncapped) as JSONL or CSV.
 * Returns the parsed filename from Content-Disposition so the caller can
 * name the saved file; the browser's own download UI handles the rest.
 */
export async function exportLlmCalls({ docId = '', runId = '', callSite = '', status = '', since = '', format = 'jsonl' } = {}) {
  const params = new URLSearchParams({ format })
  if (docId) params.set('doc_id', docId)
  if (runId) params.set('run_id', runId)
  if (callSite) params.set('call_site', callSite)
  if (status) params.set('status', status)
  if (since) params.set('since', since)
  const response = await fetch(`/api/llm/calls/export?${params}`)
  if (!response.ok) {
    const detail = await readJson(response).catch(() => ({}))
    throw new Error(detail.detail || `${response.status} ${response.statusText}`)
  }
  const disposition = response.headers.get('Content-Disposition') || ''
  const match = /filename="?([^";]+)"?/.exec(disposition)
  const blob = await response.blob()
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = match ? match[1] : `llm-calls.${format}`
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

/**
 * Download one call's complete input and output (the per-call detail file,
 * not the 800-char previews). Older calls without a detail file fall back
 * to their JSONL record server-side.
 */
export async function exportLlmCallDetail(callId) {
  const response = await fetch(`/api/llm/calls/${encodeURIComponent(callId)}/export`)
  if (!response.ok) {
    const detail = await readJson(response).catch(() => ({}))
    throw new Error(detail.detail || `${response.status} ${response.statusText}`)
  }
  const disposition = response.headers.get('Content-Disposition') || ''
  const match = /filename="?([^";]+)"?/.exec(disposition)
  const blob = await response.blob()
  downloadBlob(blob, match ? match[1] : `llm-call-${callId}.json`)
}

/** One activity-layer task with its merged activity + note timeline. */
export function fetchTaskDetail(taskId) {
  return request(`/api/tasks/${encodeURIComponent(taskId)}`)
}

export function search(query, top = 20, collection = '') {
  const params = new URLSearchParams({ q: query, top: String(top) })
  if (collection) params.set('collection', collection)
  return request(`/api/search?${params.toString()}`)
}

export function fetchNotes(params = {}) {
  const query = new URLSearchParams()
  if (params.docId) query.set('doc_id', params.docId)
  if (params.author) query.set('author', params.author)
  if (params.kind) query.set('kind', params.kind)
  if (params.issueId) query.set('issue_id', params.issueId)
  if (params.limit) query.set('limit', String(params.limit))
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return request(`/api/notes${suffix}`)
}

export function fetchResource(docId, resourceId) {
  return request(
    `/api/documents/${encodeURIComponent(docId)}/resources/${encodeURIComponent(resourceId)}`,
  )
}

export function cancelRun(id) {
  return request(`/api/runs/${encodeURIComponent(id)}/cancel`, { method: 'POST' })
}

export function addNote(note) {
  return request('/api/notes', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(note),
  })
}

/** Remove one hand-off note and its replies (human cleanup path). */
export function deleteNote(noteId) {
  return request(`/api/notes/${encodeURIComponent(noteId)}`, { method: 'DELETE' })
}

/** Remove a document, its derived rows and its files. */
export function deleteDocument(id) {
  return request(`/api/documents/${encodeURIComponent(id)}`, { method: 'DELETE' })
}

/** Set (or clear, with an empty body) a document's human-owned 入库备注. */
export function saveIngestNote(docId, body) {
  return request(`/api/documents/${encodeURIComponent(docId)}/ingest-note`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ body }),
  })
}

export function fetchEndpoints() {
  return request('/api/endpoints')
}

export function fetchOcrProviders() {
  return request('/api/ocr/providers')
}

export function fetchOcrEndpoints() {
  return request('/api/ocr/endpoints')
}

export function createOcrEndpoint(data) {
  return request('/api/ocr/endpoints', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
}

export function updateOcrEndpoint(id, data) {
  return request(`/api/ocr/endpoints/${encodeURIComponent(id)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
}

export function deleteOcrEndpoint(id) {
  return request(`/api/ocr/endpoints/${encodeURIComponent(id)}`, { method: 'DELETE' })
}

export function testOcrEndpoint(id) {
  return request(`/api/ocr/endpoints/${encodeURIComponent(id)}/test`, { method: 'POST' })
}

export function setDefaultOcrEndpoint(id) {
  return request(`/api/ocr/endpoints/${encodeURIComponent(id)}/default`, { method: 'PUT' })
}

export function probeMagpie() {
  return request('/api/endpoints/magpie/probe')
}

export function createEndpoint(data) {
  return request('/api/endpoints', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
}

export function updateEndpoint(id, data) {
  return request(`/api/endpoints/${encodeURIComponent(id)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
}

export function deleteEndpoint(id) {
  return request(`/api/endpoints/${encodeURIComponent(id)}`, { method: 'DELETE' })
}

export function testEndpoint(id) {
  return request(`/api/endpoints/${encodeURIComponent(id)}/test`, { method: 'POST' })
}

export function fetchDefaultEndpoint() {
  return request('/api/endpoints/default')
}

export function setDefaultEndpoint(id) {
  return request(`/api/endpoints/${encodeURIComponent(id)}/default`, { method: 'PUT' })
}

/** Effective 纲目 / 编修 config plus the shipped defaults (for reset/badges). */
export function fetchAgents() {
  return request('/api/agents')
}

export function updateAgent(name, data) {
  return request(`/api/agents/${encodeURIComponent(name)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
}

/** Persist the LLM call-log level (full | meta | off). */
export function updateLlmLogMode(mode) {
  return request('/api/settings/llm-log', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode }),
  })
}

export function fetchBenchScenarios() {
  return request('/api/bench')
}

export function fetchBenchScenario(id) {
  return request(`/api/bench/${encodeURIComponent(id)}`)
}

export function submitBench(docIds, endpointIds, ocrEndpointIds = []) {
  return request('/api/bench', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      doc_ids: docIds,
      endpoint_ids: endpointIds,
      ocr_endpoint_ids: ocrEndpointIds,
    }),
  })
}

export function deleteBenchScenario(id) {
  return request(`/api/bench/${encodeURIComponent(id)}`, { method: 'DELETE' })
}

// ── collections (topic areas) ──

export function fetchCollections() {
  return request('/api/collections')
}

export function createCollection({ name, description = '' }) {
  return request('/api/collections', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, description }),
  })
}

export function updateCollection(id, patch) {
  return request(`/api/collections/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  })
}

export function deleteCollection(id) {
  return request(`/api/collections/${encodeURIComponent(id)}`, { method: 'DELETE' })
}

export function assignDocuments({ docIds, collectionId = null }) {
  return request('/api/documents/collection', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ docIds, collectionId }),
  })
}

// ── uploads ──

/** Stream one file to the server; ingest runs in the background. */
export function uploadDocument({
  file,
  collectionId = null,
  imageMode = 'structured',
  ingestNote = '',
}) {
  const form = new FormData()
  form.append('file', file)
  if (collectionId) form.append('collectionId', collectionId)
  // Images only: structured (TOC/figures) or text (one flat searchable doc).
  form.append('imageMode', imageMode)
  // Human-written 入库备注, attached to the document after ingest.
  if (ingestNote) form.append('ingestNote', ingestNote)
  return request('/api/uploads', { method: 'POST', body: form })
}

export function fetchUpload(id) {
  return request(`/api/uploads/${encodeURIComponent(id)}`)
}

// ── agent console (runs) ──

/** Start one research run; the response is the run row, steps arrive later. */
export function startRun({ question, allowNotes = true, priorRunId = null, maxCalls = 9999, maxTokens = 0 }) {
  return request('/api/runs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question, allowNotes, priorRunId, maxCalls, maxTokens }),
  })
}

export function fetchRuns(limit = 20, source = '') {
  const query = new URLSearchParams({ limit: String(limit) })
  if (source) query.set('source', source)
  return request(`/api/runs?${query}`)
}

export function fetchRun(id) {
  return request(`/api/runs/${encodeURIComponent(id)}`)
}

/** Every version of a note: rev 1 (original) through the current one. */
export function fetchNoteHistory(noteId) {
  return request(`/api/notes/${encodeURIComponent(noteId)}/history`)
}

/** Append a revision to a note (the previous body stays in the chain). */
export function reviseNote(noteId, payload) {
  return request(`/api/notes/${encodeURIComponent(noteId)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

/** Hide a note from the default list without deleting its history. */
export function retractNote(noteId, payload = {}) {
  return request(`/api/notes/${encodeURIComponent(noteId)}/retract`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}
