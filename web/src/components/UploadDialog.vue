<script setup>
/**
 * Upload documents into a collection. The files go to the server, which
 * streams them to disk and runs the normal ingest pipeline in the
 * background; this dialog only shows that pipeline's status. Closing the
 * dialog does not cancel anything — the document still appears in the
 * library when ingest finishes.
 */
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { fetchUpload, uploadDocument } from '@/api'
import { useCollectionsStore } from '@/stores/collections'
import { useUiStore } from '@/stores/ui'
import UiButton from '@/components/ui/UiButton.vue'
import UiSeg from '@/components/ui/UiSeg.vue'

const props = defineProps({
  open: { type: Boolean, default: false },
  initialCollection: { type: String, default: '' },
  // Retry prefill from 解析队列: { file, collectionId, imageMode, ingestNote }.
  initial: { type: Object, default: null },
})
const emit = defineEmits(['close', 'done'])

const collections = useCollectionsStore()
const ui = useUiStore()

const ACCEPT = [
  '.pdf', '.doc', '.docx', '.md', '.markdown', '.txt',
  '.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff', '.gif',
].join(',')
// Spreadsheet upload is paused for now; the server rejects it too.
const BLOCKED = ['.xls', '.xlsx', '.csv', '.tsv']
const MAX_FILES = 20
const POLL_MS = 1500

const target = ref('')
const imageMode = ref('structured')
const ingestNote = ref('')
const items = ref([])
const dragging = ref(false)
const uploading = ref(false)
const fileInput = ref(null)
let runToken = 0
// Give up polling a single row after this many consecutive fetch failures so a
// permanently failing status endpoint cannot keep the loop spinning at 1.5 s
// forever (see the poll loop in start()).
const MAX_POLL_FAILURES = 3

// The dialog can unmount mid-upload (route change, HMR). Bump the run token so
// the in-flight poll loop bails out on its next check instead of leaking a
// timer that polls a dead component.
onUnmounted(() => {
  runToken += 1
})

watch(
  () => props.open,
  (open) => {
    if (!open) {
      runToken += 1
      return
    }
    const retry = props.initial
    const wanted = retry?.collectionId || props.initialCollection
    target.value = wanted && collections.byId(wanted) ? wanted : ''
    items.value = []
    uploading.value = false
    dragging.value = false
    imageMode.value = retry?.imageMode === 'text' ? 'text' : 'structured'
    ingestNote.value = retry?.ingestNote || ''
    nextTick(() => {
      if (fileInput.value) fileInput.value.value = ''
      // A retry re-opens with the original File object so the user does
      // not have to find the file again.
      if (retry?.file) addFiles([retry.file])
    })
  },
)

const pending = computed(() => items.value.filter((item) => item.status === 'pending'))
const targetName = computed(() => (target.value ? collections.byId(target.value)?.name : '未分区'))

const IMAGE_SUFFIXES = ['.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff', '.gif']
const hasImages = computed(() =>
  items.value.some((item) =>
    IMAGE_SUFFIXES.some((suffix) => item.name.toLowerCase().endsWith(suffix)),
  ),
)
const imageModes = [
  { value: 'structured', label: '结构化' },
  { value: 'text', label: '纯文本' },
]

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function statusLabel(item) {
  return {
    pending: '待上传',
    uploading: '上传中',
    queued: '排队中',
    indexing: '解析中',
    ready: '已入库',
    duplicate: '库中已有',
    failed: '失败',
  }[item.status] || item.status
}

function addFiles(list) {
  const known = new Set(items.value.map((item) => `${item.name}:${item.size}`))
  let rejected = 0
  let blocked = 0
  for (const file of Array.from(list || [])) {
    const key = `${file.name}:${file.size}`
    if (known.has(key)) continue
    if (BLOCKED.some((suffix) => file.name.toLowerCase().endsWith(suffix))) {
      blocked += 1
      continue
    }
    if (items.value.length >= MAX_FILES) {
      rejected += 1
      continue
    }
    known.add(key)
    items.value.push({
      file,
      name: file.name,
      size: file.size,
      status: 'pending',
      uploadId: '',
      docId: '',
      error: '',
      message: '',
    })
  }
  if (blocked) ui.notify('Excel / CSV 上传暂未开放，请先导出为 PDF 或文本后再上传')
  if (rejected) ui.notify(`一次最多 ${MAX_FILES} 个文件，其余未加入`)
}

function onPick(event) {
  addFiles(event.target.files)
  event.target.value = ''
}

function onDrop(event) {
  dragging.value = false
  addFiles(event.dataTransfer?.files)
}

function removeItem(index) {
  if (uploading.value) return
  items.value.splice(index, 1)
}

/** Put a failed row back in the pending queue; 开始上传 will send it again. */
function retryItem(item) {
  item.status = 'pending'
  item.uploadId = ''
  item.docId = ''
  item.error = ''
  item.message = ''
}

function close() {
  runToken += 1
  emit('close')
}

async function start() {
  if (uploading.value || !pending.value.length) return
  uploading.value = true
  const token = ++runToken
  const queue = [...pending.value]

  for (const item of queue) {
    if (token !== runToken) return
    item.status = 'uploading'
    try {
      const data = await uploadDocument({
        file: item.file,
        collectionId: target.value || null,
        imageMode: imageMode.value,
        ingestNote: ingestNote.value.trim(),
      })
      item.uploadId = data.upload.id
      item.status = data.upload.status || 'queued'
    } catch (error) {
      item.status = 'failed'
      item.error = error.message
    }
  }

  while (token === runToken) {
    const active = items.value.filter(
      (item) => item.uploadId && ['queued', 'indexing', 'uploading'].includes(item.status),
    )
    if (!active.length) break
    await new Promise((resolve) => setTimeout(resolve, POLL_MS))
    if (token !== runToken) return
    await Promise.all(
      active.map(async (item) => {
        try {
          const data = await fetchUpload(item.uploadId)
          item.status = data.status
          item.docId = data.docId || ''
          item.error = data.error || ''
          item.message = data.message || ''
          item.pollFailures = 0
        } catch (error) {
          // A transient error keeps the row in the poll set; a persistent one
          // is terminal so the loop can stop instead of hammering the server.
          item.pollFailures = (item.pollFailures || 0) + 1
          if (item.pollFailures >= MAX_POLL_FAILURES) {
            item.status = 'failed'
            item.error = error.message
          } else {
            item.error = error.message
          }
        }
      }),
    )
  }

  if (token !== runToken) return
  uploading.value = false
  const ready = items.value.filter((item) => item.status === 'ready').length
  const duplicate = items.value.filter((item) => item.status === 'duplicate').length
  const failed = items.value.filter((item) => item.status === 'failed').length
  if (ready || duplicate) {
    emit('done')
    const parts = []
    if (ready) parts.push(`入库 ${ready}`)
    if (duplicate) parts.push(`已存在 ${duplicate}`)
    if (failed) parts.push(`失败 ${failed}`)
    ui.notify(`${parts.join(' · ')}${targetName.value ? ` · ${targetName.value}` : ''}`)
  }
}
</script>

<template>
  <Teleport to="body">
    <div v-if="props.open" class="modal-backdrop" @click.self="close">
      <div class="modal upload-modal">
        <div class="modal-head">
          <div>
            <h2>上传文档</h2>
            <p>解析在后台进行，关闭窗口不会中断。</p>
          </div>
          <button class="modal-close" @click="close">×</button>
        </div>

        <label class="target-row">
          <span>目标分区</span>
          <select v-model="target" :disabled="uploading">
            <option value="">未分区</option>
            <option v-for="item in collections.items" :key="item.id" :value="item.id">
              {{ item.name }}
            </option>
          </select>
        </label>

        <div v-if="hasImages" class="mode-row">
          <span class="mode-label">图片处理</span>
          <UiSeg
            v-model="imageMode"
            :items="imageModes"
            size="sm"
            aria-label="图片处理方式"
          />
          <span class="mode-hint">
            {{ imageMode === 'structured'
              ? '建目录、抽图表，适合版面复杂的图'
              : '只留原图和识别文字（仍走 OCR），适合截图、聊天记录' }}
          </span>
        </div>

        <label class="note-field">
          <span class="note-label">入库备注</span>
          <textarea
            v-model="ingestNote"
            rows="2"
            maxlength="20000"
            placeholder="可选：这份文件是什么、从哪来、打算怎么用…（agent 可读，之后可在知识库页修改）"
          ></textarea>
          <span class="note-hint">本批每份文档都会写入这条备注；上传后可在知识库页修改。</span>
        </label>

        <div
          class="dropzone"
          :class="{ dragging }"
          @click="fileInput?.click()"
          @dragover.prevent="dragging = true"
          @dragleave="dragging = false"
          @drop.prevent="onDrop"
        >
          <input
            ref="fileInput"
            type="file"
            multiple
            :accept="ACCEPT"
            class="file-input"
            @change="onPick"
          >
          <div class="dropzone-title">拖入文件，或点击选择</div>
          <div class="dropzone-hint">PDF / Word / Markdown / TXT / 图片 · 单文件 ≤ 200 MB</div>
        </div>

        <div v-if="items.length" class="upload-list">
          <div v-for="(item, index) in items" :key="item.uploadId || `${item.name}:${item.size}`" class="upload-row">
            <div class="upload-main">
              <span class="upload-name">{{ item.name }}</span>
              <span class="upload-size">{{ formatSize(item.size) }}</span>
            </div>
            <span class="upload-status" :class="item.status">{{ statusLabel(item) }}</span>
            <button
              v-if="item.status === 'pending' && !uploading"
              class="upload-remove"
              title="移除"
              @click="removeItem(index)"
            >×</button>
            <button
              v-else-if="item.status === 'failed' && !uploading"
              class="upload-retry"
              title="用同样的文件与设置再传一次"
              @click="retryItem(item)"
            >重试</button>
            <div v-if="item.error" class="upload-note error">{{ item.error }}</div>
            <div v-else-if="item.message" class="upload-note">{{ item.message }}</div>
          </div>
        </div>

        <div class="modal-actions">
          <UiButton size="md" @click="close">{{ uploading ? '后台继续' : '关闭' }}</UiButton>
          <UiButton
            variant="primary"
            size="md"
            :disabled="uploading || !pending.length"
            @click="start"
          >
            {{ uploading ? '上传中…' : `开始上传${pending.length ? ` · ${pending.length}` : ''}` }}
          </UiButton>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.upload-modal { width: min(560px, 100%); }
.target-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 16px;
  font-size: 12px;
  color: var(--muted);
}
.target-row select {
  flex: 1;
  height: var(--ctl-h);
  border: 1px solid var(--border);
  border-radius: var(--ctl-radius);
  background: var(--surface);
  color: var(--text);
  padding: 0 8px;
  font-size: 12px;
}
.mode-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 10px;
  font-size: 12px;
  color: var(--muted);
  flex-wrap: wrap;
}
.mode-label { flex: none; }
.mode-hint { font-size: 10.5px; color: var(--faint); }
.note-field { display: block; margin-top: 12px; }
.note-label { display: block; font-size: 12px; color: var(--muted); margin-bottom: 6px; }
.note-field textarea {
  width: 100%;
  box-sizing: border-box;
  resize: vertical;
  min-height: 52px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: var(--ctl-radius);
  background: var(--surface);
  color: var(--text);
  font: inherit;
  font-size: 12px;
  line-height: 1.55;
}
.note-field textarea:focus { outline: none; border-color: var(--accent); box-shadow: var(--focus-ring); }
.note-hint { display: block; margin-top: 4px; font-size: 10px; color: var(--faint); }
.dropzone {
  margin-top: 12px;
  padding: 22px 16px;
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius);
  background: var(--surface-2);
  text-align: center;
  cursor: pointer;
  transition: border-color .12s var(--ease), background .12s var(--ease);
}
.dropzone:hover,
.dropzone.dragging {
  border-color: var(--accent);
  background: var(--accent-soft);
}
.file-input { display: none; }
.dropzone-title { font-size: 12.5px; color: var(--text); }
.dropzone-hint { margin-top: 5px; font-size: 10.5px; color: var(--faint); }
.upload-list {
  margin-top: 12px;
  max-height: 260px;
  overflow: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}
.upload-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  gap: 8px;
  align-items: center;
  padding: 8px 10px;
  border-bottom: 1px solid var(--border);
}
.upload-row:last-child { border-bottom: 0; }
.upload-main { min-width: 0; display: flex; align-items: baseline; gap: 8px; }
.upload-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  color: var(--text);
}
.upload-size { flex: none; font-size: 10px; color: var(--faint); }
.upload-status { font-size: 10.5px; color: var(--muted); white-space: nowrap; }
.upload-status.ready { color: var(--accent); }
.upload-status.failed { color: var(--danger); }
.upload-status.duplicate { color: var(--warn, #b45309); }
.upload-remove {
  width: 20px;
  height: 20px;
  border: 0;
  border-radius: 4px;
  background: none;
  color: var(--faint);
  cursor: pointer;
}
.upload-remove:hover { background: var(--pill-hover); color: var(--text); }
.upload-retry {
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--accent);
  border-radius: 5px;
  padding: 1px 8px;
  font-size: 10.5px;
  cursor: pointer;
}
.upload-retry:hover { border-color: var(--accent); background: var(--accent-soft); }
.upload-note {
  grid-column: 1 / -1;
  font-size: 10.5px;
  color: var(--faint);
  word-break: break-word;
}
.upload-note.error { color: var(--danger); }
</style>