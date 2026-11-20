<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { fetchOcrProviders } from '@/api'
import { useOcrStore } from '@/stores/ocr'
import UiButton from '@/components/ui/UiButton.vue'

const ocr = useOcrStore()

const PROVIDER_LABELS = {
  paddle: 'PaddleOCR-VL',
  mistral: 'Mistral OCR',
  mineru: 'MinerU',
  azure: 'Azure 文档智能',
}
const PROVIDER_NOTES = {
  paddle: '结构化 block，目录/页码识别最准',
  mistral: 'Markdown + 图片 bbox，按量便宜',
  mineru: '可私有化，输出 content_list.json',
  azure: '标题语义强（sectionHeading）',
}

const providers = ref([])
const providerDefaults = ref({})
const emptyForm = () => ({
  name: '',
  provider: 'paddle',
  base_url: '',
  api_key: '',
  model: '',
})
const form = reactive(emptyForm())
const editingId = ref(null) // null = closed, '' = new, id = editing
const saving = ref(false)
const testingId = ref(null)
const testResults = reactive({}) // id -> {ok, text}
const error = ref('')

const providerOptions = computed(() =>
  providers.value.map((row) => ({
    ...row,
    label: PROVIDER_LABELS[row.name] || row.name,
    note: PROVIDER_NOTES[row.name] || '',
  })),
)

const activeLabel = computed(() => {
  if (!ocr.active) return '未配置'
  const row = ocr.endpoints.find((item) => item.name === ocr.active.name)
  return row ? row.name : ocr.active.name
})

onMounted(async () => {
  await ocr.load()
  try {
    const catalog = await fetchOcrProviders()
    providers.value = catalog.providers || []
    providerDefaults.value = Object.fromEntries(
      (catalog.providers || []).map((row) => [row.name, row]),
    )
  } catch {
    providers.value = []
  }
})

function applyProviderDefaults() {
  const defaults = providerDefaults.value[form.provider]
  if (!defaults) return
  form.base_url = defaults.base_url || ''
  form.model = defaults.model || ''
}

function openCreate() {
  editingId.value = ''
  Object.assign(form, emptyForm())
  applyProviderDefaults()
  error.value = ''
}

function openEdit(row) {
  editingId.value = row.id
  Object.assign(form, {
    name: row.name,
    provider: row.provider,
    base_url: row.base_url,
    api_key: '',
    model: row.model,
  })
  error.value = ''
}

function cancel() {
  editingId.value = null
  error.value = ''
}

async function save() {
  saving.value = true
  error.value = ''
  try {
    if (editingId.value === '') {
      await ocr.create({ ...form })
    } else {
      await ocr.update(editingId.value, { ...form })
    }
    cancel()
  } catch (e) {
    error.value = e.message
  } finally {
    saving.value = false
  }
}

async function test(row) {
  testingId.value = row.id
  try {
    const r = await ocr.test(row.id)
    testResults[row.id] = r.ok
      ? { ok: true, text: `连通 · ${r.elapsed_ms}ms` }
      : { ok: false, text: `失败 · ${r.error}` }
  } catch (e) {
    testResults[row.id] = { ok: false, text: e.message }
  } finally {
    testingId.value = null
  }
}

async function makeDefault(row) {
  await ocr.makeDefault(row.id)
}

async function remove(row) {
  if (!confirm(`删除 OCR 端点「${row.name}」？`)) return
  await ocr.remove(row.id)
}
</script>

<template>
  <div class="panel">
    <div class="panel-head">
      <div>
        <h3>OCR</h3>
        <span class="panel-sub">入库时的扫描件解析；默认端点优先，.env 作为回落</span>
      </div>
      <div class="head-actions">
        <span class="active-hint">当前：{{ activeLabel }}</span>
        <UiButton size="sm" v-if="editingId === null" @click="openCreate">新增端点</UiButton>
      </div>
    </div>

    <details class="ocr-about">
      <summary>OCR 后端如何选择？</summary>
      <p>
        只有 PDF 文本层不可用时才走 OCR。默认端点优先；未设默认时用
        <code>.env</code> 里的 <code>TOCMEMEX_OCR_PROVIDER</code> 与其密钥。
        归一化结果缓存在 <code>data/ocr/&lt;doc_id&gt;/pages.json</code>，
        切换后端不影响已入库文档的 TOC 与切片。
      </p>
    </details>

    <div v-if="editingId !== null" class="panel-body ocr-form">
      <div class="form-grid">
        <label><span>名称</span><input v-model="form.name" type="text" placeholder="paddle-main"></label>
        <label>
          <span>后端</span>
          <select v-model="form.provider" @change="applyProviderDefaults">
            <option v-for="p in providerOptions" :key="p.name" :value="p.name">
              {{ p.label }}
            </option>
          </select>
        </label>
        <label class="wide"><span>Base URL</span><input v-model="form.base_url" type="text" placeholder="https://…"></label>
        <label>
          <span>模型 / 版本</span>
          <input v-model="form.model" type="text" placeholder="PaddleOCR-VL-1.6">
        </label>
        <label>
          <span>API Key{{ editingId !== '' ? '（留空保持不变）' : '' }}</span>
          <input v-model="form.api_key" type="password" placeholder="token / key">
        </label>
      </div>
      <div v-if="error" class="form-error">{{ error }}</div>
      <div class="form-actions">
        <UiButton variant="primary" size="sm" :disabled="saving || !form.name || !form.base_url"
          @click="save"
        >{{ saving ? '保存中…' : '保存' }}</UiButton>
        <UiButton variant="ghost" size="sm" @click="cancel">取消</UiButton>
      </div>
    </div>

    <div v-else class="panel-body ocr-list">
      <p v-if="!ocr.endpoints.length" class="hint">
        还没有 OCR 端点。新增一个，或在 .env 里配置 TOCMEMEX_OCR_PROVIDER 作为回落。
      </p>
      <div
        v-for="row in ocr.endpoints"
        :key="row.id"
        class="ocr-card"
        :class="{ default: row.is_default }"
      >
        <div class="ocr-main">
          <div class="ocr-title">
            <strong>{{ row.name }}</strong>
            <span class="badge">{{ PROVIDER_LABELS[row.provider] || row.provider }}</span>
            <span v-if="row.is_default" class="badge accent">默认</span>
            <span v-if="!row.enabled" class="badge">停用</span>
          </div>
          <div class="ocr-meta mono">{{ row.base_url }}</div>
          <div class="ocr-meta">
            {{ row.model || '—' }} · key {{ row.has_key ? `····${row.key_tail}` : '未配置' }}
          </div>
        </div>
        <div class="ocr-side">
          <span class="test-result" :class="{ ok: testResults[row.id]?.ok, bad: testResults[row.id]?.ok === false }">
            {{ testResults[row.id]?.text }}
          </span>
          <div class="ocr-actions">
            <UiButton size="sm" :disabled="testingId === row.id" @click="test(row)">
              {{ testingId === row.id ? '测试中…' : '测试' }}
            </UiButton>
            <UiButton size="sm" v-if="!row.is_default" @click="makeDefault(row)">设默认</UiButton>
            <UiButton variant="ghost" size="sm" @click="openEdit(row)">编辑</UiButton>
            <UiButton variant="ghost" size="sm" @click="remove(row)">删除</UiButton>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.head-actions { display: flex; align-items: center; gap: 8px; }
.active-hint { font-size: 11px; color: var(--faint); }
.ocr-about {
  padding: 10px 16px;
  border-bottom: 1px solid var(--border);
  font-size: 11.5px;
  color: var(--muted);
}
.ocr-about summary {
  cursor: pointer;
  font-size: 11px;
  color: var(--faint);
  user-select: none;
}
.ocr-about summary:hover { color: var(--accent); }
.ocr-about p { margin: 8px 0 0; line-height: 1.65; }
.ocr-about code {
  font-family: var(--mono);
  font-size: 10.5px;
  background: var(--surface-2);
  border-radius: 3px;
  padding: 0 4px;
}

.ocr-form .form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px 14px;
}
.ocr-form .wide { grid-column: span 2; }
.ocr-form label { display: flex; flex-direction: column; gap: 4px; font-size: 11px; color: var(--muted); }
.ocr-form input,
.ocr-form select {
  height: 28px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
  padding: 0 9px;
  font-size: 11.5px;
  outline: none;
}
.ocr-form input:focus,
.ocr-form select:focus { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-soft); }
.form-actions { margin-top: 12px; display: flex; gap: 8px; }
.form-error { margin-top: 10px; font-size: 11.5px; color: var(--danger); }

.ocr-list { display: flex; flex-direction: column; gap: 10px; }
.ocr-card {
  display: flex; align-items: center; justify-content: space-between; gap: 16px;
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  padding: 10px 14px;
}
.ocr-card.default { border-color: color-mix(in srgb, var(--accent) 35%, var(--border)); }
.ocr-title { display: flex; align-items: center; gap: 8px; font-size: 12.5px; }
.ocr-meta { font-size: 11px; color: var(--muted); margin-top: 3px; overflow: hidden; text-overflow: ellipsis; }
.ocr-side { display: flex; flex-direction: column; align-items: flex-end; gap: 6px; flex: none; }
.test-result { font-size: 10.5px; color: var(--faint); max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.test-result.ok { color: var(--ok); }
.test-result.bad { color: var(--danger); }
.ocr-actions { display: flex; gap: 6px; }
</style>
