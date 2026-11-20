<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { probeMagpie } from '@/api'
import { useEndpointsStore } from '@/stores/endpoints'
import UiButton from '@/components/ui/UiButton.vue'

const endpoints = useEndpointsStore()

const emptyForm = () => ({ name: '', base_url: '', api_key: '', model: '', reasoning_effort: '' })
const form = reactive(emptyForm())
const editingId = ref(null) // null = create form closed / hidden, '' = new, id = editing
const saving = ref(false)
const testingId = ref(null)
const testResults = reactive({}) // id -> {ok, text}
const error = ref('')
const magpieModels = ref(null) // null = manual input; [] or list = suggestions
const probing = ref(false)
const magpieUnavailable = ref(false)

const ALL_LEVELS = ['none', 'low', 'medium', 'high']

// magpie onboarding: restrict levels to what the selected model supports
const effortOptions = computed(() => {
  if (magpieModels.value && form.model) {
    const picked = magpieModels.value.find((m) => m.id === form.model)
    if (picked && picked.reasoning_levels && picked.reasoning_levels.length) {
      return picked.reasoning_levels
    }
  }
  return ALL_LEVELS
})

onMounted(() => endpoints.load())

function openCreate() {
  editingId.value = ''
  Object.assign(form, emptyForm())
  magpieModels.value = null
  error.value = ''
}

async function onboardMagpie() {
  probing.value = true
  magpieUnavailable.value = false
  try {
    const probe = await probeMagpie()
    if (!probe.available) {
      magpieUnavailable.value = true
      return
    }
    editingId.value = ''
    error.value = ''
    Object.assign(form, {
      name: 'magpie',
      base_url: probe.base_url,
      api_key: 'magpie-tocmemex',
      model: probe.models.length ? probe.models[0].id : '',
      reasoning_effort: '',
    })
    magpieModels.value = probe.models
  } catch {
    magpieUnavailable.value = true
  } finally {
    probing.value = false
  }
}

function openEdit(row) {
  editingId.value = row.id
  Object.assign(form, {
    name: row.name,
    base_url: row.base_url,
    api_key: '',
    model: row.model,
    reasoning_effort: row.reasoning_effort || '',
  })
  magpieModels.value = null
  error.value = ''
}

function cancel() {
  editingId.value = null
  magpieModels.value = null
  error.value = ''
}

async function save() {
  saving.value = true
  error.value = ''
  try {
    if (editingId.value === '') {
      await endpoints.create({ ...form })
    } else {
      await endpoints.update(editingId.value, { ...form })
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
    const r = await endpoints.test(row.id)
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
  await endpoints.makeDefault(row.id)
}

async function remove(row) {
  if (!confirm(`删除端点「${row.name}」？`)) return
  await endpoints.remove(row.id)
}
</script>

<template>
  <div class="panel">
    <div class="panel-head">
      <div>
        <h3>模型端点</h3>
        <span class="panel-sub">OpenAI 兼容接口；key 只保存在服务端</span>
      </div>
      <div class="head-actions">
        <span v-if="magpieUnavailable" class="hint">未检测到 magpie（127.0.0.1:3425）</span>
        <UiButton size="sm" :disabled="probing || editingId !== null"
          @click="onboardMagpie"
        >{{ probing ? '探测中…' : '接入 magpie' }}</UiButton>
        <UiButton size="sm" v-if="editingId === null" @click="openCreate">新增端点</UiButton>
      </div>
    </div>

    <details class="endpoint-about">
      <summary>LLM 在本项目里做什么？</summary>
      <p>
        LLM 用于两处：① 入库时目录解析——规则解析结果可疑（根节点过少、乱序或有校验错误）时，
        调 LLM 生成目录提案并与规则结果打分仲裁；② 对比台——把规则 TOC 与各端点 LLM TOC 打分对比。
      </p>
      <p>
        未配置任何端点且 <code>.env</code> 无 <code>TOCMEMEX_LLM_*</code> 时，项目完全正常运行：
        入库走纯规则路径，仅对比台提交会提示 400（需先设默认端点）。LLM 失败永不阻断入库，自动降级回规则结果。
      </p>
    </details>

    <div v-if="editingId !== null" class="panel-body endpoint-form">
      <div class="form-grid">
        <label><span>名称</span><input v-model="form.name" type="text" placeholder="gpt-main"></label>
        <label><span>Base URL</span><input v-model="form.base_url" type="text" placeholder="https://api.example.com/v1"></label>
        <label>
          <span>模型</span>
          <input v-model="form.model" type="text" list="magpie-model-options" placeholder="gpt-4o">
          <datalist v-if="magpieModels && magpieModels.length" id="magpie-model-options">
            <option v-for="m in magpieModels" :key="m.id" :value="m.id">{{ m.display_name || m.id }}</option>
          </datalist>
        </label>
        <label>
          <span>推理力度（空 = 供应商默认）</span>
          <select v-model="form.reasoning_effort" class="effort-select">
            <option value="">默认</option>
            <option v-for="lv in effortOptions" :key="lv" :value="lv">{{ lv }}</option>
          </select>
        </label>
        <label>
          <span>API Key{{ editingId !== '' ? '（留空保持不变）' : '' }}</span>
          <input v-model="form.api_key" type="password" placeholder="sk-...">
        </label>
      </div>
      <div v-if="error" class="form-error">{{ error }}</div>
      <div class="form-actions">
        <UiButton variant="primary" size="sm" :disabled="saving || !form.name || !form.base_url || !form.model" @click="save">
          {{ saving ? '保存中…' : '保存' }}
        </UiButton>
        <UiButton variant="ghost" size="sm" @click="cancel">取消</UiButton>
      </div>
    </div>

    <div v-else class="panel-body endpoint-list">
      <p v-if="!endpoints.endpoints.length" class="hint">还没有端点。新增一个，或在 .env 里配置 TOCMEMEX_LLM_* 作为回落。</p>
      <div v-for="row in endpoints.endpoints" :key="row.id" class="endpoint-card" :class="{ default: row.is_default }">
        <div class="endpoint-main">
          <div class="endpoint-title">
            <strong>{{ row.name }}</strong>
            <span v-if="row.is_default" class="badge accent">默认</span>
            <span v-if="!row.enabled" class="badge">停用</span>
          </div>
          <div class="endpoint-meta mono">{{ row.base_url }}</div>
          <div class="endpoint-meta">
            {{ row.model }} · key {{ row.has_key ? `····${row.key_tail}` : '未配置' }}
            <template v-if="row.reasoning_effort"> · 推理力度 {{ row.reasoning_effort }}</template>
          </div>
        </div>
        <div class="endpoint-side">
          <span class="test-result" :class="{ ok: testResults[row.id]?.ok, bad: testResults[row.id]?.ok === false }">
            {{ testResults[row.id]?.text }}
          </span>
          <div class="endpoint-actions">
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
.head-actions .hint { font-size: 11px; color: var(--faint); }
.endpoint-about {
  padding: 10px 16px;
  border-bottom: 1px solid var(--border);
  font-size: 11.5px;
  color: var(--muted);
}
.endpoint-about summary {
  cursor: pointer;
  font-size: 11px;
  color: var(--faint);
  user-select: none;
}
.endpoint-about summary:hover { color: var(--accent); }
.endpoint-about p { margin: 8px 0 0; line-height: 1.65; }
.endpoint-about code {
  font-family: var(--mono);
  font-size: 10.5px;
  background: var(--surface-2);
  border-radius: 3px;
  padding: 0 4px;
}
.endpoint-form .form-grid {
  display: grid;
  grid-template-columns: 1fr 2fr;
  gap: 10px 14px;
}
.endpoint-form label { display: flex; flex-direction: column; gap: 4px; font-size: 11px; color: var(--muted); }
.endpoint-form input {
  height: 28px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
  padding: 0 9px;
  font-size: 11.5px;
  outline: none;
}
.endpoint-form input:focus,
.endpoint-form select:focus { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-soft); }
.endpoint-form select {
  height: 28px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
  padding: 0 6px;
  font-size: 11.5px;
  outline: none;
}
.form-actions { margin-top: 12px; display: flex; gap: 8px; }
.form-error { margin-top: 10px; font-size: 11.5px; color: var(--danger); }

.endpoint-list { display: flex; flex-direction: column; gap: 10px; }
.endpoint-card {
  display: flex; align-items: center; justify-content: space-between; gap: 16px;
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  padding: 10px 14px;
}
.endpoint-card.default { border-color: color-mix(in srgb, var(--accent) 35%, var(--border)); }
.endpoint-title { display: flex; align-items: center; gap: 8px; font-size: 12.5px; }
.endpoint-meta { font-size: 11px; color: var(--muted); margin-top: 3px; overflow: hidden; text-overflow: ellipsis; }
.endpoint-side { display: flex; flex-direction: column; align-items: flex-end; gap: 6px; flex: none; }
.test-result { font-size: 10.5px; color: var(--faint); max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.test-result.ok { color: var(--ok); }
.test-result.bad { color: var(--danger); }
.endpoint-actions { display: flex; gap: 6px; }
</style>
