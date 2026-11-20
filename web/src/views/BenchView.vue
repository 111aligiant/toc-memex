<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { storeToRefs } from 'pinia'
import DocTable from '@/components/DocTable.vue'
import { useLibraryStore } from '@/stores/library'
import { useEndpointsStore } from '@/stores/endpoints'
import { useOcrStore } from '@/stores/ocr'
import { useBenchStore } from '@/stores/bench'
import UiEmpty from '@/components/ui/UiEmpty.vue'
import UiButton from '@/components/ui/UiButton.vue'

const library = useLibraryStore()
const endpoints = useEndpointsStore()
const ocr = useOcrStore()
const bench = useBenchStore()
const { query, filteredDocs, documents } = storeToRefs(library)

const selectedDocs = ref([]) // doc ids
const selectedEndpoints = ref([]) // endpoint ids
const selectedOcr = ref(['cached']) // ocr endpoint ids or 'cached' | 'text'
const submitting = ref(false)
const error = ref('')

onMounted(async () => {
  await Promise.all([library.load(), endpoints.load(), ocr.load(), bench.load()])
})

onUnmounted(() => bench.stopPoll())

const detail = computed(() => bench.detail)
const docTitle = computed(() => {
  const id = detail.value?.scenario?.doc_ids?.[0]
  return documents.value.find((d) => d.id === id)?.title || id || ''
})

const ocrChoices = computed(() => [
  { id: 'cached', name: '已有解析缓存', hint: '不重新 OCR' },
  { id: 'text', name: 'PDF 文本层', hint: '不调用 OCR' },
  ...ocr.endpoints.map((ep) => ({
    id: ep.id,
    name: ep.name,
    hint: `${ep.provider}${ep.model ? ` · ${ep.model}` : ''}${ep.is_default ? ' · 默认' : ''}`,
  })),
])

const combinationCount = computed(
  () => selectedDocs.value.length * selectedOcr.value.length * selectedEndpoints.value.length,
)

function toggleDoc(id) {
  const i = selectedDocs.value.indexOf(id)
  if (i >= 0) selectedDocs.value.splice(i, 1)
  else selectedDocs.value.push(id)
}

function toggleEndpoint(id) {
  const i = selectedEndpoints.value.indexOf(id)
  if (i >= 0) selectedEndpoints.value.splice(i, 1)
  else selectedEndpoints.value.push(id)
}

function toggleOcr(id) {
  const i = selectedOcr.value.indexOf(id)
  if (i >= 0) selectedOcr.value.splice(i, 1)
  else selectedOcr.value.push(id)
}

function selectAllVisible() {
  selectedDocs.value = [...new Set([...selectedDocs.value, ...filteredDocs.value.map((d) => d.id)])]
}

function clearDocs() {
  selectedDocs.value = []
}

async function submit() {
  submitting.value = true
  error.value = ''
  try {
    await bench.submit(
      [...selectedDocs.value],
      [...selectedEndpoints.value],
      [...selectedOcr.value],
    )
    selectedDocs.value = []
    selectedEndpoints.value = []
    selectedOcr.value = ['cached']
  } catch (e) {
    error.value = e.message
  } finally {
    submitting.value = false
  }
}

async function openScenario(row) {
  await bench.open(row.id)
}

function statusLabel(s) {
  return { processing: '进行中', done: '完成', failed: '失败' }[s] || s
}

function fmtMs(ms) {
  if (ms == null) return '—'
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`
  return `${ms}ms`
}

function fmtTime(iso) {
  if (!iso) return '—'
  return iso.slice(11, 19)
}

function fmtCoverage(c) {
  return c == null ? '—' : `${Math.round(c * 100)}%`
}

function docName(id) {
  return documents.value.find((d) => d.id === id)?.title || id.slice(0, 8)
}

// One row per (document, OCR choice, endpoint): rule + llm side by side.
const detailRows = computed(() => {
  if (!detail.value) return []
  const groups = new Map()
  for (const run of detail.value.runs) {
    const key = `${run.doc_id}|${run.ocr_endpoint_id || 'cached'}`
    if (!groups.has(key)) groups.set(key, [])
    groups.get(key).push(run)
  }
  const out = []
  for (const [key, runs] of groups) {
    const rule = runs.find((r) => r.side === 'rule')
    const llms = runs.filter((r) => r.side === 'llm')
    if (!llms.length) {
      out.push({ key, rule, llm: null })
      continue
    }
    for (const llm of llms) out.push({ key: `${key}|${llm.id}`, rule, llm })
  }
  return out
})

function scoreBetter(llm, rule) {
  if (!llm || llm.status !== 'ok' || !rule || rule.status !== 'ok') return false
  const l = llm.score || [0, 0, 0, 0]
  const r = rule.score || [0, 0, 0, 0]
  for (let i = 0; i < 4; i++) {
    if (l[i] !== r[i]) return l[i] > r[i]
  }
  return false
}

function verdict(row) {
  if (row.rule?.status === 'error') return { text: 'OCR 失败', cls: 'delta-down' }
  if (!row.llm) return { text: '无 LLM 运行', cls: '' }
  if (row.llm.status === 'error') return { text: '调用失败', cls: '' }
  if (scoreBetter(row.llm, row.rule)) return { text: 'LLM 更优', cls: 'delta-up' }
  if (JSON.stringify(row.llm.score) === JSON.stringify(row.rule?.score)) return { text: '持平', cls: '' }
  return { text: '规则更优', cls: 'delta-down' }
}

const showErrors = reactive({})

function toggleError(row) {
  const key = row.key
  showErrors[key] = !showErrors[key]
}
</script>

<template>
  <section class="page">
    <div class="page-head">
      <div>
        <h1>对比</h1>
        <p>文档 × OCR × LLM 三者组合对比：同一份文档换 OCR、换模型，规则与 LLM 目录并排打分。只写对比记录，不影响知识库。</p>
      </div>
    </div>

    <!-- 文档选择：复用知识库表格 -->
    <div class="panel">
      <div class="panel-head">
        <h3>选择文档</h3>
        <span class="panel-sub">已选 {{ selectedDocs.length }} / {{ documents.length }}</span>
      </div>
      <div class="toolbar">
        <div class="search-box">
          <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
          <input v-model="query" type="search" placeholder="筛选标题、关键词…">
        </div>
        <div class="toolbar-spacer"></div>
        <UiButton variant="ghost" size="sm" @click="selectAllVisible">全选当前 {{ filteredDocs.length }}</UiButton>
        <UiButton variant="ghost" size="sm" :disabled="!selectedDocs.length" @click="clearDocs">清空</UiButton>
      </div>
      <div class="doc-pick-wrap">
        <DocTable
          :docs="filteredDocs"
          :selected-ids="selectedDocs"
          @open="doc => toggleDoc(doc.id)"
          @toggle-select="toggleDoc"
        />
      </div>
      <UiEmpty
        v-if="!filteredDocs.length"
        title="没有匹配的文档"
        hint="调整筛选条件或清空搜索。"
      />
    </div>

    <!-- 组合 -->
    <div class="panel">
      <div class="panel-head">
        <h3>选择 OCR 与 LLM</h3>
        <span class="panel-sub">OCR 每个文档只跑一次，结果按组合复用</span>
      </div>
      <div class="panel-body">
        <div class="pick-title">OCR（可多选）</div>
        <div class="chip-row">
          <label
            v-for="choice in ocrChoices"
            :key="choice.id"
            class="endpoint-chip"
            :class="{ on: selectedOcr.includes(choice.id) }"
          >
            <input type="checkbox" :checked="selectedOcr.includes(choice.id)" @change="toggleOcr(choice.id)">
            {{ choice.name }}
            <span class="key">{{ choice.hint }}</span>
          </label>
        </div>

        <div class="pick-title">LLM 端点（可多选）</div>
        <div class="chip-row">
          <label
            v-for="ep in endpoints.endpoints"
            :key="ep.id"
            class="endpoint-chip"
            :class="{ on: selectedEndpoints.includes(ep.id) }"
          >
            <input type="checkbox" :checked="selectedEndpoints.includes(ep.id)" @change="toggleEndpoint(ep.id)">
            {{ ep.name }}
            <span class="key">{{ ep.model }}{{ ep.is_default ? ' · 默认' : '' }}</span>
          </label>
          <span v-if="!endpoints.endpoints.length" class="hint">还没有端点 —— 先到设置页添加</span>
        </div>

        <div v-if="error" class="submit-error">{{ error }}</div>
        <div class="submit-row">
          <UiButton variant="primary" size="md" :disabled="submitting || !selectedDocs.length || !selectedEndpoints.length || !selectedOcr.length"
            @click="submit"
          >
            {{ submitting ? '提交中…' : '开始对比' }}
          </UiButton>
          <span class="hint">
            {{ selectedDocs.length }} 文档 × {{ selectedOcr.length }} OCR × {{ selectedEndpoints.length }} 端点
            = {{ combinationCount }} 组
          </span>
        </div>
      </div>
    </div>

    <!-- 场景列表 -->
    <div class="panel list-panel">
      <div class="panel-head">
        <h3>对比记录</h3>
        <span class="panel-sub">进行中的场景每 3 秒自动刷新</span>
      </div>
      <div class="table-wrap">
        <table class="data">
          <thead>
            <tr>
              <th>场景</th><th>文档数</th><th>OCR</th><th>端点</th>
              <th>状态</th><th>创建时间</th><th>结束时间</th><th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="!bench.scenarios.length">
              <td colspan="8" class="faint td-empty">还没有对比记录</td>
            </tr>
            <tr
              v-for="s in bench.scenarios"
              :key="s.id"
              :class="{ selected: detail?.scenario?.id === s.id }"
            >
              <td class="mono">{{ s.id.slice(0, 8) }}</td>
              <td class="num">{{ s.doc_ids.length }}</td>
              <td class="num">{{ (s.ocr_endpoint_ids || ['cached']).length }}</td>
              <td class="num">{{ s.endpoint_ids.length }}</td>
              <td>
                <span class="status" :class="s.status"><span class="dot"></span>{{ statusLabel(s.status) }}</span>
                <span v-if="s.error" class="faint" :title="s.error"> · {{ s.error.slice(0, 40) }}</span>
              </td>
              <td class="faint">{{ fmtTime(s.created_at) }}</td>
              <td class="faint">{{ fmtTime(s.finished_at) }}</td>
              <td class="row-actions" @click.stop>
                <UiButton variant="ghost" size="sm" @click="openScenario(s)">查看</UiButton>
                <UiButton variant="ghost" size="sm" @click="bench.remove(s.id)">删除</UiButton>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 场景详情 -->
    <div v-if="detail" class="panel">
      <div class="panel-head">
        <div>
          <h3>{{ detail.scenario.id.slice(0, 8) }}<template v-if="docTitle"> · {{ docTitle }}</template></h3>
          <span class="panel-sub">规则 TOC 为基线，LLM 得分更好时整行高亮</span>
        </div>
        <UiButton variant="ghost" size="sm" @click="bench.close()">收起</UiButton>
      </div>
      <div class="table-wrap">
        <table class="data">
          <thead>
            <tr>
              <th>文档</th><th>OCR</th><th>端点</th>
              <th class="num">校验错误</th><th class="num">警告</th>
              <th class="num">节点数</th><th class="num">覆盖率</th>
              <th class="num">OCR 耗时</th><th class="num">LLM 耗时</th><th>结果</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="!detailRows.length">
              <td colspan="10" class="faint td-empty">还没有 run 数据</td>
            </tr>
            <tr v-for="row in detailRows" :key="row.key" :class="{ 'llm-better': verdict(row).cls === 'delta-up' }">
              <td>{{ docName(row.rule?.doc_id || row.llm?.doc_id) }}</td>
              <td>
                {{ row.rule?.ocr_label || row.llm?.ocr_label || '—' }}
                <span v-if="(row.rule || row.llm)?.ocr_status === 'error'" class="danger"> · 失败</span>
              </td>
              <td>
                {{ row.llm?.endpoint_name || '—' }}
                <span v-if="row.llm?.model" class="faint mono"> · {{ row.llm.model }}</span>
              </td>
              <td class="num">
                <template v-if="row.llm?.status === 'ok'">
                  <span class="score-pair"><span class="rule">{{ row.rule?.validate_errors ?? '—' }} →</span> {{ row.llm.validate_errors }}</span>
                </template>
                <template v-else>{{ row.rule?.validate_errors ?? '—' }}</template>
              </td>
              <td class="num">
                <template v-if="row.llm?.status === 'ok'">
                  <span class="score-pair"><span class="rule">{{ row.rule?.validate_warnings ?? '—' }} →</span> {{ row.llm.validate_warnings }}</span>
                </template>
                <template v-else>{{ row.rule?.validate_warnings ?? '—' }}</template>
              </td>
              <td class="num">
                <template v-if="row.llm?.status === 'ok'">
                  <span class="score-pair"><span class="rule">{{ row.rule?.node_count ?? '—' }} →</span> {{ row.llm.node_count }}</span>
                </template>
                <template v-else>{{ row.rule?.node_count ?? '—' }}</template>
              </td>
              <td class="num">
                <template v-if="row.llm?.status === 'ok'">
                  <span class="score-pair"><span class="rule">{{ fmtCoverage(row.rule?.coverage) }} →</span> {{ fmtCoverage(row.llm.coverage) }}</span>
                </template>
                <template v-else>{{ fmtCoverage(row.rule?.coverage) }}</template>
              </td>
              <td class="num">{{ fmtMs(row.rule?.ocr_elapsed_ms ?? row.llm?.ocr_elapsed_ms) }}</td>
              <td class="num">{{ fmtMs(row.llm?.elapsed_ms) }}</td>
              <td>
                <span class="verdict" :class="verdict(row).cls">{{ verdict(row).text }}</span>
                <UiButton
                  v-if="row.llm?.status === 'error' || row.rule?.status === 'error'"
                  variant="quiet"
                  size="sm"
                  class="ml-8"
                  @click="toggleError(row)"
                >详情</UiButton>
                <div v-if="showErrors[row.key]" class="run-error mono">{{ row.llm?.error || row.rule?.error }}</div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="panel-body hint">
        得分口径：校验错误最少 → 警告最少 → 有页码的节点最多 → 页面覆盖率最高；逐项比较，分出胜负即停。
      </div>
    </div>
  </section>
</template>

<style scoped>
.doc-pick-wrap { max-height: 420px; overflow: auto; border-top: 1px solid var(--border); }
.pick-title { font-size: 11px; text-transform: uppercase; letter-spacing: .08em; color: var(--faint); font-weight: 600; margin-bottom: 8px; }
.pick-title:not(:first-child) { margin-top: 14px; }
.chip-row { display: flex; gap: 8px; flex-wrap: wrap; }
.endpoint-chip {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 10px; border: 1px solid var(--border); border-radius: var(--radius-sm);
  font-size: 11.5px; color: var(--muted); background: var(--surface);
}
.endpoint-chip.on { color: var(--text); border-color: var(--accent); background: var(--accent-soft); }
.endpoint-chip .key { font-size: 10px; color: var(--faint); }
.endpoint-chip input { accent-color: var(--accent); margin: 0; }
.submit-row { margin-top: 14px; display: flex; align-items: center; gap: 10px; }
.submit-error { margin-top: 10px; font-size: 11.5px; color: var(--danger); }
.list-panel { margin-top: 14px; }
.row-actions { white-space: nowrap; }
tr.selected td { background: var(--accent-soft); }
.score-pair .rule { color: var(--faint); }
tr.llm-better td { background: var(--ok-soft); }
.delta-up { color: var(--ok); font-size: 11.5px; }
.delta-down { color: var(--danger); font-size: 11.5px; }
.verdict { font-size: 11.5px; color: var(--muted); }
.run-error { margin-top: 4px; font-size: 10px; color: var(--danger); word-break: break-all; white-space: normal; max-width: 340px; }
.danger { color: var(--danger); }
</style>
