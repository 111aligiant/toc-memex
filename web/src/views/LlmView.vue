<script setup>
import { computed, onMounted, ref } from 'vue'
import { exportLlmCallDetail, exportLlmCalls, fetchLlmCalls, fetchLlmUsage } from '@/api'
import { absTime, relTime } from '@/utils/format'
import UiEmpty from '@/components/ui/UiEmpty.vue'
import UiPanel from '@/components/ui/UiPanel.vue'
import UiChip from '@/components/ui/UiChip.vue'
import UiButton from '@/components/ui/UiButton.vue'

/**
 * LLM 用量（ADR-0002 §10.4）。
 * 回答一个问题：每份文档花了多少 token、走了几步、换来多少改进。
 * 柱状图用纯 CSS，不引图表库（UI 约定）。
 *
 * 筛选和分页都在服务端：明细可能远超一页，客户端过滤会在
 * 「最新 100 条里找不到旧 run」时静默给出空表。
 */
const PAGE_SIZE = 100

const usage = ref(null)
const calls = ref([])
const totalCalls = ref(null) // 服务端记录总数（来自一次无筛选探查）
const loading = ref(true)
const loadingMore = ref(false)
const error = ref('')
const filterRun = ref('')
const expanded = ref(null)
const exporting = ref('')
const exportingCall = ref('')

onMounted(load)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [summary] = await Promise.all([fetchLlmUsage()])
    usage.value = summary
    await reloadCalls()
  } catch (err) {
    error.value = err.message || '读取失败'
  } finally {
    loading.value = false
  }
}

/** 当前筛选下的第一页；筛选变化时调用。 */
async function reloadCalls() {
  expanded.value = null
  const list = await fetchLlmCalls({
    runId: filterRun.value,
    limit: PAGE_SIZE,
    offset: 0,
  })
  calls.value = list.calls || []
  totalCalls.value = list.total ?? null
}

/** 服务端分页的「加载更多」：追加下一页。 */
async function loadMore() {
  if (loadingMore.value || !hasMore.value) return
  loadingMore.value = true
  try {
    const list = await fetchLlmCalls({
      runId: filterRun.value,
      limit: PAGE_SIZE,
      offset: calls.value.length,
    })
    calls.value = [...calls.value, ...(list.calls || [])]
    totalCalls.value = list.total ?? totalCalls.value
  } catch (err) {
    error.value = err.message || '读取失败'
  } finally {
    loadingMore.value = false
  }
}

async function pickRun(runId) {
  filterRun.value = filterRun.value === runId ? '' : runId
  loading.value = true
  try {
    await reloadCalls()
  } catch (err) {
    error.value = err.message || '读取失败'
  } finally {
    loading.value = false
  }
}

async function doExport(format) {
  exporting.value = format
  error.value = ''
  try {
    await exportLlmCalls({ runId: filterRun.value, format })
  } catch (err) {
    error.value = err.message || '导出失败'
  } finally {
    exporting.value = ''
  }
}

/** 导出单次调用的完整输入输出（明细文件，非 800 字预览）。 */
async function doExportCall(call) {
  exportingCall.value = call.id
  error.value = ''
  try {
    await exportLlmCallDetail(call.id)
  } catch (err) {
    error.value = err.message || '导出失败'
  } finally {
    exportingCall.value = ''
  }
}

const totals = computed(() => usage.value?.total || { calls: 0, tokens_in: 0, tokens_out: 0, errors: 0 })

const byDay = computed(() => {
  const rows = usage.value?.by_day || []
  const max = Math.max(1, ...rows.map((row) => row.tokens))
  return rows.map((row) => ({ ...row, height: Math.round((row.tokens / max) * 100) }))
})

// unassigned 不是一次运行，是「没带 run_id 的调用」的兜底桶；把它排除，
// 运行数才是真实的运行数。
const byRun = computed(() => (usage.value?.by_run || []).filter((run) => run.run_id !== 'unassigned'))
const unassignedRun = computed(() => (usage.value?.by_run || []).find((run) => run.run_id === 'unassigned'))
const byEndpoint = computed(() => usage.value?.by_endpoint || [])

const hasMore = computed(() =>
  totalCalls.value != null ? calls.value.length < totalCalls.value : false,
)

function tokens(call) {
  const value = call.tokens || {}
  return value.total ?? ((value.prompt || 0) + (value.completion || 0))
}

function tokenTone(call) {
  return (call.tokens || {}).source === 'estimated' ? 'warn' : ''
}
</script>

<template>
  <section class="page">
    <div class="page-head">
      <div>
        <h1>LLM 用量</h1>
        <p>每次调用的 token、耗时与工具调用。数据来自 data/llm/calls.jsonl。</p>
      </div>
      <div class="head-actions">
        <UiButton size="sm" variant="ghost" :disabled="exporting !== ''" @click="doExport('csv')">
          {{ exporting === 'csv' ? '导出中…' : '导出 CSV' }}
        </UiButton>
        <UiButton size="sm" variant="ghost" :disabled="exporting !== ''" @click="doExport('jsonl')">
          {{ exporting === 'jsonl' ? '导出中…' : '导出 JSONL' }}
        </UiButton>
        <UiButton size="sm" @click="load">刷新</UiButton>
      </div>
    </div>

    <div v-if="error" class="panel"><div class="panel-body danger-text">{{ error }}</div></div>

    <template v-else>
      <div class="stats">
        <div class="stat">
          <div class="stat-label">总调用</div>
          <div class="stat-value">{{ totals.calls }}</div>
          <div class="stat-meta">失败 {{ totals.errors }}</div>
        </div>
        <div class="stat">
          <div class="stat-label">输入 token</div>
          <div class="stat-value">{{ totals.tokens_in.toLocaleString() }}</div>
          <div class="stat-meta">prompt</div>
        </div>
        <div class="stat">
          <div class="stat-label">输出 token</div>
          <div class="stat-value">{{ totals.tokens_out.toLocaleString() }}</div>
          <div class="stat-meta">completion</div>
        </div>
        <div class="stat">
          <div class="stat-label">运行数</div>
          <div class="stat-value">{{ byRun.length }}</div>
          <div class="stat-meta">另有 {{ unassignedRun ? unassignedRun.calls : 0 }} 次调用未归属运行</div>
        </div>
      </div>

      <div class="llm-grid">
        <UiPanel title="按天" :sub="byDay.length ? `${byDay.length} 天` : ''">
          <div v-if="byDay.length" class="day-bars">
            <div v-for="row in byDay" :key="row.day" class="day-bar">
              <div class="bar-wrap">
                <div class="bar" :style="{ height: row.height + '%' }" :title="`${row.tokens.toLocaleString()} token · ${row.calls} 次调用`"></div>
              </div>
              <div class="bar-label">{{ row.day.slice(5) }}</div>
              <div class="bar-value">{{ row.tokens.toLocaleString() }}</div>
            </div>
          </div>
          <UiEmpty v-else compact title="还没有调用记录" />
        </UiPanel>

        <UiPanel title="按运行" flush>
          <div class="table-wrap">
            <table class="data">
              <thead><tr><th>run</th><th>文档</th><th class="num">调用</th><th class="num">token</th><th class="num">错误</th></tr></thead>
              <tbody>
                <tr
                  v-for="run in byRun"
                  :key="run.run_id"
                  :class="{ active: filterRun === run.run_id }"
                  @click="pickRun(run.run_id)"
                >
                  <td class="mono">{{ run.run_id }}</td>
                  <td class="mono">{{ run.doc_id ? run.doc_id.slice(0, 8) : '—' }}</td>
                  <td class="num">{{ run.calls }}</td>
                  <td class="num">{{ run.tokens.toLocaleString() }}</td>
                  <td class="num">{{ run.errors }}</td>
                </tr>
                <tr v-if="!byRun.length"><td colspan="5"><UiEmpty compact title="还没有带 run_id 的调用" hint="curator agent 运行时会带上 run_id；入库时的目录解析没有。" /></td></tr>
              </tbody>
            </table>
          </div>
        </UiPanel>

        <UiPanel v-if="byEndpoint.length > 1" title="按端点" flush class="span-2">
          <div class="table-wrap">
            <table class="data">
              <thead><tr><th>端点</th><th>模型</th><th class="num">调用</th><th class="num">token</th><th class="num">错误</th></tr></thead>
              <tbody>
                <tr v-for="ep in byEndpoint" :key="ep.endpoint">
                  <td class="mono">{{ ep.endpoint }}</td>
                  <td class="mono">{{ ep.model }}</td>
                  <td class="num">{{ ep.calls }}</td>
                  <td class="num">{{ ep.tokens.toLocaleString() }}</td>
                  <td class="num">{{ ep.errors }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </UiPanel>
      </div>

      <UiPanel :title="filterRun ? `调用明细 · run ${filterRun}` : '调用明细'" :sub="totalCalls != null ? `共 ${totalCalls} 条，已加载 ${calls.length}` : `${calls.length} 条`" flush>
        <template v-if="filterRun" #actions>
          <UiButton size="sm" variant="ghost" @click="pickRun(filterRun)">清除筛选</UiButton>
        </template>
        <div class="table-wrap">
          <table class="data">
            <thead><tr><th>时间</th><th>调用点</th><th>模型</th><th class="num">token</th><th class="num">耗时</th><th>状态</th></tr></thead>
            <tbody>
              <template v-for="call in calls" :key="call.id">
                <tr class="call-row" @click="expanded = expanded === call.id ? null : call.id">
                  <td class="faint">{{ relTime(call.ts) }}</td>
                  <td class="mono">{{ call.call_site }}</td>
                  <td class="mono">{{ call.model }}</td>
                  <td class="num">
                    {{ tokens(call).toLocaleString() }}
                    <UiChip v-if="tokenTone(call)" :tone="tokenTone(call)" size="sm">估算</UiChip>
                  </td>
                  <td class="num">{{ call.elapsed_ms }} ms</td>
                  <td>
                    <span class="status" :class="call.status === 'ok' ? 'ready' : 'failed'">
                      <span class="dot"></span>{{ call.status === 'ok' ? '成功' : '失败' }}
                    </span>
                  </td>
                </tr>
                <tr v-if="expanded === call.id" class="call-detail">
                  <td colspan="6">
                    <div v-if="call.error" class="detail-block danger-text">{{ call.error }}</div>
                    <div class="detail-head">
                      <span class="detail-label">请求 / 返回预览</span>
                      <UiButton size="sm" variant="ghost" :disabled="exportingCall !== ''" @click.stop="doExportCall(call)">
                        {{ exportingCall === call.id ? '导出中…' : '导出完整输入输出' }}
                      </UiButton>
                    </div>
                    <pre class="mono detail-pre">{{ call.request?.prompt_preview || '（未记录）' }}</pre>
                    <div class="detail-label">返回预览</div>
                    <pre class="mono detail-pre">{{ call.response?.content_preview || '（空）' }}</pre>
                    <div v-if="call.request?.tools?.length" class="detail-label">工具</div>
                    <div v-if="call.request?.tools?.length" class="detail-tools">
                      <UiChip v-for="name in call.request.tools" :key="name" size="sm">{{ name }}</UiChip>
                    </div>
                    <div v-if="call.response?.tool_calls?.length" class="detail-label">本次工具调用</div>
                    <pre v-if="call.response?.tool_calls?.length" class="mono detail-pre">{{ JSON.stringify(call.response.tool_calls, null, 2) }}</pre>
                    <div class="detail-meta">{{ absTime(call.ts) }} · run {{ call.run_id || '—' }} · doc {{ call.doc_id || '—' }}</div>
                  </td>
                </tr>
              </template>
              <tr v-if="!calls.length && !loading"><td colspan="6"><UiEmpty compact title="没有调用记录" hint="配置 LLM 并运行一次 curator 后，这里会出现记录。" /></td></tr>
            </tbody>
          </table>
        </div>
        <div v-if="hasMore" class="load-more">
          <UiButton size="sm" variant="ghost" :disabled="loadingMore" @click="loadMore">
            {{ loadingMore ? '加载中…' : `加载更多（还剩 ${(totalCalls - calls.length).toLocaleString()} 条）` }}
          </UiButton>
        </div>
      </UiPanel>
    </template>
  </section>
</template>

<style scoped>
.head-actions { display: flex; gap: 8px; align-items: center; }
.danger-text { color: var(--danger); font-size: 12px; }
.llm-grid { display: grid; grid-template-columns: minmax(0, .8fr) minmax(0, 1.2fr); gap: 14px; margin-top: 14px; }
.llm-grid .span-2 { grid-column: 1 / -1; }
@media (max-width: 1000px) { .llm-grid { grid-template-columns: 1fr; } }
.day-bars { display: flex; align-items: flex-end; gap: 6px; height: 150px; padding-top: 8px; }
.day-bar { flex: 1 1 0; max-width: 56px; display: flex; flex-direction: column; align-items: center; gap: 4px; min-width: 0; }
.bar-wrap { width: 100%; height: 110px; display: flex; align-items: flex-end; }
.bar { width: 100%; min-height: 2px; background: color-mix(in srgb, var(--accent) 55%, var(--surface-3)); border-radius: 2px 2px 0 0; }
.bar-label { font-size: 9.5px; color: var(--faint); }
.bar-value { font-size: 9.5px; color: var(--muted); white-space: nowrap; }
.call-row { cursor: pointer; }
.call-row.active, .data tbody tr.active { background: var(--pill); }
.call-detail td { background: var(--surface-2); padding: 12px 14px; }
.detail-label { font-size: 10px; color: var(--faint); margin: 8px 0 3px; }
.detail-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.detail-head .detail-label { margin: 0; }
.detail-pre { font-size: 10.5px; white-space: pre-wrap; word-break: break-word; max-height: 220px; overflow: auto; margin: 0; color: var(--muted); }
.detail-block { font-size: 11px; margin-bottom: 6px; }
.detail-tools { display: flex; flex-wrap: wrap; gap: 4px; }
.detail-meta { font-size: 10px; color: var(--faint); margin-top: 10px; }
.load-more { display: flex; justify-content: center; padding: 10px 0 12px; border-top: 1px solid var(--border); }
</style>
