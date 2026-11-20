<script setup>
import { computed, onMounted, ref } from 'vue'
import { useWorkspaceStore } from '@/stores/workspace'
import { useUiStore } from '@/stores/ui'
import { absTime, formatBytes, statusLabel } from '@/utils/format'
import { fetchBaselines, restoreBaseline } from '@/api'
import UiEmpty from '@/components/ui/UiEmpty.vue'
import UiPanel from '@/components/ui/UiPanel.vue'
import UiButton from '@/components/ui/UiButton.vue'
import UiChip from '@/components/ui/UiChip.vue'

const workspace = useWorkspaceStore()
const ui = useUiStore()

// ── baselines (ADR-0002 §4.4) ──
const baselines = ref([])
const baselineLoading = ref(false)
const baselineError = ref('')
const restoring = ref(false)

const latestBaseline = computed(() => baselines.value[0] || null)

async function loadBaselines() {
  if (!workspace.docId) return
  baselineLoading.value = true
  baselineError.value = ''
  try {
    const payload = await fetchBaselines(workspace.docId)
    baselines.value = payload.baselines || []
  } catch (err) {
    baselineError.value = err.message || '基线读取失败'
  } finally {
    baselineLoading.value = false
  }
}

onMounted(loadBaselines)

async function onRestore(force = false, target = null) {
  const row = target || latestBaseline.value
  if (!row) return
  const when = absTime(row.created_at)
  const prompt = force
    ? '文档已重新入库，这份基线对应旧版本。仍要恢复吗？'
    : `将丢弃 curator 对这份文档的全部修改，恢复到 ${when} 的基线。继续？`
  if (!window.confirm(prompt)) return
  restoring.value = true
  try {
    const result = await restoreBaseline(workspace.docId, {
      baselineId: row.id,
      force,
    })
    ui.notify(`已恢复到 ${when} 的基线`)
    await workspace.load(workspace.docId)
    await loadBaselines()
    return result
  } catch (err) {
    const message = err.message || '恢复失败'
    if (message.includes('content hash mismatch')) {
      await onRestore(true, row)
    } else {
      ui.notify(message)
    }
  } finally {
    restoring.value = false
  }
}

function onRestoreTo(row) {
  return onRestore(false, row)
}

const baselineRows = computed(() => {
  const row = latestBaseline.value
  if (!row) return []
  return [
    ['建立时间', `${absTime(row.created_at)} · ${reasonLabel(row.reason)}`],
    ['节点 / 切片', `${row.node_count} / ${row.chunk_count}`],
    ['对应版本', `${row.content_hash.slice(0, 8)}…（${row.matches_current ? '与当前一致' : '旧版本'}）`],
  ]
})

const REASON = {
  adoption: '接入前自动建立',
  pre_curator_write: 'curator 写入前',
  manual: '手动建立',
}

function reasonLabel(reason) {
  return REASON[reason] || reason || '—'
}

const infoRows = computed(() => {
  const doc = workspace.document
  if (!doc) return []
  return [
    ['文件名', doc.filename],
    ['文档类型', (doc.type || '').toUpperCase()],
    ['文件大小', formatBytes(doc.size)],
    ['页数', String(doc.page_count ?? '—')],
    ['目录节点', String(workspace.stats?.nodes ?? '—')],
    ['切片数', String(workspace.stats?.chunks ?? '—')],
    ['非正文页', String(workspace.stats?.nonContentPages ?? '—')],
    ['状态', statusLabel(doc.status)],
    ['更新时间', absTime(doc.updated_at)],
  ]
})

const current = computed(() => workspace.provenance?.current || null)
const history = computed(() => workspace.provenance?.history || [])

const LLM_OUTCOME = {
  accepted: '采用 LLM 结果',
  rejected: '未采用（规则更优）',
  no_proposal: '无有效提案',
  error: '调用失败',
  not_needed: '未触发（规则结果健康）',
  unavailable: '未配置 LLM',
}

const CHANNEL = {
  toc_page: '印刷目录页',
  pageindex_md: 'OCR 标题',
  bookmark: 'PDF 书签',
  printed_toc: '印刷目录页（规则）',
  printed_toc_llm: '印刷目录页（LLM）',
  fallback: '兜底单节点',
  // parse_channel values (ingest provenance), not TOC sources.
  'pdf-text': 'PDF 文本层',
  ocr: 'OCR 结构化',
  'ocr-text': 'OCR 纯文本',
  'docx-direct': 'DOCX 直读',
  markdown: 'Markdown 直读',
  table: '表格直读',
}

function channelLabel(source) {
  return CHANNEL[source] || source || '—'
}

const ocrRows = computed(() => {
  const run = current.value
  if (!run) return []
  const ocr = run.ocr
  if (ocr.used) {
    return [
      ['是否使用', '是'],
      ['Provider', ocr.provider || '未记录'],
      ['模型', ocr.model || '未记录（旧缓存）'],
      ['端点', ocr.endpoint || '—'],
      ['缓存', ocr.cache || '—'],
      ['耗时', ocr.elapsed_ms != null ? `${(ocr.elapsed_ms / 1000).toFixed(1)}s` : '—'],
    ]
  }
  return [
    ['是否使用', '否'],
    ['走的路径', channelLabel(run.parse_channel) || '文本层'],
    ['说明', '该文档直接解析文本，未调用 OCR'],
  ]
})

const llmRows = computed(() => {
  const run = current.value
  if (!run) return []
  const llm = run.llm
  if (llm.outcome == null) {
    return [['是否使用', '未记录'], ['说明', '该文档入库早于溯源记录，无法确认']]
  }
  if (!llm.used) {
    return [['是否使用', '否'], ['结果', LLM_OUTCOME[llm.outcome] || llm.outcome]]
  }
  return [
    ['是否使用', '是'],
    ['端点', llm.endpoint || '.env 配置'],
    ['模型', llm.model || '未记录'],
    ['结果', LLM_OUTCOME[llm.outcome] || llm.outcome || '—'],
    ['耗时', llm.elapsed_ms != null ? `${(llm.elapsed_ms / 1000).toFixed(1)}s` : '—'],
  ]
})
</script>

<template>
  <div class="settings-grid">
    <div class="panel">
      <div class="panel-head"><h3>文档信息</h3></div>
      <div class="panel-body">
        <div v-for="row in infoRows" :key="row[0]" class="setting-row">
          <span class="label">{{ row[0] }}</span>
          <span class="value">{{ row[1] }}</span>
        </div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-head">
        <h3>入库溯源</h3>
        <span v-if="current" class="panel-sub">{{ current.recorded_by === 'backfill' ? '按缓存回填' : current.recorded_by === 'reindex' ? '重建索引' : '入库记录' }}</span>
      </div>
      <div class="panel-body">
        <template v-if="current">
          <div class="setting-row">
            <div>
              <div class="label">原始文件</div>
              <div class="desc mono file-path">{{ current.file.path }}</div>
            </div>
            <span class="value" :class="current.file.exists ? '' : 'danger'">
              {{ current.file.exists ? formatBytes(current.file.size) : '文件缺失' }}
            </span>
          </div>
          <div class="setting-row">
            <span class="label">TOC 来源</span>
            <span class="value">{{ channelLabel(current.toc_source) }}</span>
          </div>
          <div class="stat-meta mt-14">OCR</div>
          <div v-for="row in ocrRows" :key="row[0]" class="setting-row">
            <span class="label">{{ row[0] }}</span>
            <span class="value">{{ row[1] }}</span>
          </div>
          <div class="stat-meta mt-14">LLM 目录解析</div>
          <div v-for="row in llmRows" :key="row[0]" class="setting-row">
            <span class="label">{{ row[0] }}</span>
            <span class="value">{{ row[1] }}</span>
          </div>
          <div class="setting-row">
            <span class="label">记录时间</span>
            <span class="value">{{ absTime(current.created_at) }}</span>
          </div>
        </template>
        <UiEmpty v-else title="还没有溯源记录">
          <div class="hint">运行 <span class="mono">python -m app.cli provenance --backfill</span> 回填存量文档。</div>
        </UiEmpty>
      </div>
    </div>

    <UiPanel title="数据基线" :sub="latestBaseline ? 'curator 介入前的状态' : ''">
      <template v-if="baselineLoading">
        <div class="baseline-note">读取中…</div>
      </template>
      <template v-else-if="latestBaseline">
        <div v-for="row in baselineRows" :key="row[0]" class="setting-row">
          <span class="label">{{ row[0] }}</span>
          <span class="value">{{ row[1] }}</span>
        </div>
        <div class="baseline-actions">
          <UiButton
            variant="danger"
            size="sm"
            :disabled="restoring"
            @click="onRestore(false)"
          >
            {{ restoring ? '恢复中…' : '恢复到此基线' }}
          </UiButton>
          <UiChip v-if="!latestBaseline.matches_current" tone="warn" size="sm">
            文档已重新入库
          </UiChip>
          <span class="baseline-hint">将丢弃 curator 对这份文档的全部修改</span>
        </div>
        <div v-if="baselines.length > 1" class="stat-meta mt-14">历史基线</div>
        <div v-for="row in baselines.slice(1)" :key="row.id" class="baseline-history">
          <span>{{ absTime(row.created_at) }}</span>
          <span class="faint">{{ reasonLabel(row.reason) }}</span>
          <span>{{ row.node_count }} / {{ row.chunk_count }}</span>
          <UiButton variant="ghost" size="sm" :disabled="restoring" @click="onRestoreTo(row)">
            恢复
          </UiButton>
        </div>
      </template>
      <UiEmpty v-else compact title="尚未建立基线">
        <div class="hint">运行 <span class="mono">python -m app.cli baseline create --all</span> 为存量文档建立基线。</div>
      </UiEmpty>
      <div v-if="baselineError" class="baseline-error">{{ baselineError }}</div>
    </UiPanel>

    <div class="panel">
      <div class="panel-head"><h3>索引配置</h3></div>
      <div class="panel-body">
        <div class="setting-row"><span class="label">切片上限</span><span class="value">1200 字符</span></div>
        <div class="setting-row"><span class="label">重叠</span><span class="value">100 字符</span></div>
        <div class="setting-row"><span class="label">封面 / 目录</span><span class="value">不参与切片</span></div>
        <div class="stat-meta mt-14">解析缓存</div>
        <pre class="mono mono-block">data/ocr/&lt;doc_id&gt;/pages.json
data/ocr/&lt;doc_id&gt;/raw.jsonl
重新索引不调用 OCR：
python -m app.cli reindex &lt;doc_id&gt;</pre>
      </div>
    </div>

    <div v-if="history.length > 1" class="panel">
      <div class="panel-head"><h3>入库历史</h3><span class="panel-sub">{{ history.length }} 条</span></div>
      <div class="panel-body">
        <div v-for="run in history" :key="run.id" class="history-row">
          <div class="history-when">
            <span>{{ absTime(run.created_at) }}</span>
            <span class="faint">{{ run.recorded_by }}</span>
          </div>
          <div class="history-what">
            <span>OCR：{{ run.ocr.used ? (run.ocr.provider || 'ocr') : '未用' }}</span>
            <span>LLM：{{ run.llm.outcome ? (LLM_OUTCOME[run.llm.outcome] || run.llm.outcome) : '未记录' }}</span>
            <span>TOC：{{ channelLabel(run.toc_source) }}</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.file-path { word-break: break-all; font-size: 10px; line-height: 1.5; margin-top: 2px; }
.danger { color: var(--danger); }
.history-row { padding: 8px 0; border-bottom: 1px solid var(--border); }
.history-row:last-child { border-bottom: 0; }
.history-when { display: flex; justify-content: space-between; font-size: 11.5px; }
.history-what { display: flex; gap: 12px; flex-wrap: wrap; font-size: 11px; color: var(--muted); margin-top: 3px; }
.baseline-note { font-size: 11.5px; color: var(--faint); padding: 6px 0; }
.baseline-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding-top: 12px;
}
.baseline-hint { font-size: 10.5px; color: var(--faint); }
.baseline-history {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 0;
  font-size: 11px;
  color: var(--muted);
  border-bottom: 1px solid var(--border);
}
.baseline-history:last-child { border-bottom: 0; }
.baseline-history .faint { color: var(--faint); flex: 1; }
.baseline-error { font-size: 11px; color: var(--danger); padding-top: 8px; }
</style>
