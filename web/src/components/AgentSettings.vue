<script setup>
/**
 * 纲目 / 编修 agent 的设置面板。
 *
 * 一个组件服务两个 agent：字段集合与提示词卡片由 props.name 决定，
 * 默认值、校验规则都在后端（app/llm/purpose.py）——前端只做编辑和
 * "已修改 / 恢复默认"的展示，提示词文本永远不在前端复制一份。
 *
 * 保存是显式的：改动先落在本地 draft，底部条出现后才落库。
 * 页面上的说明文字解释的是后端真实行为（预算、冷却、回落），
 * 不是装饰性帮助文本。
 */
import { computed, nextTick, onMounted, ref } from 'vue'
import { fetchAgents, updateAgent } from '@/api'
import { useEndpointsStore } from '@/stores/endpoints'
import UiButton from '@/components/ui/UiButton.vue'
import UiChip from '@/components/ui/UiChip.vue'
import UiInput from '@/components/ui/UiInput.vue'
import UiPanel from '@/components/ui/UiPanel.vue'
import UiSwitch from '@/components/ui/UiSwitch.vue'

const props = defineProps({
  name: { type: String, required: true }, // gangmu | bianxiu
})

const META = {
  gangmu: {
    mark: '纲',
    title: '纲目',
    pinyin: 'gāngmù · TOC 解析',
    desc: '文档入库时读取目录页，把印刷目录转成结构化条目并校验页码。LLM 失败时自动回落规则解析，不会卡住入库。',
    duties: ['目录页检测', '条目抽取', '页码校验', '页码修正'],
  },
  bianxiu: {
    mark: '编',
    title: '编修',
    pinyin: 'biānxiū · 维护巡检',
    desc: '周期巡检文档质量告警，用工具读页面和切片、按证据提交修正提案。高风险操作只成提案待人工批准，提示词改不掉代码里的证据核对。',
    duties: ['切片合并 / 拆分', '目录归位', '摘要修订', 'OCR 更正'],
  },
}

const PROMPTS = {
  gangmu: [
    { key: 'detect', name: '目录页检测', desc: '逐页判断是否目录页，返回 yes / no', rows: 8, vars: ['content'] },
    { key: 'transform', name: '条目抽取', desc: '整本目录一次性转 JSON（structure / title / page）', rows: 12, appended: '目录正文' },
    { key: 'verify', name: '页码校验', desc: '抽样核对"标题是否真的出现在声称的页上"', rows: 8, vars: ['title', 'page_text'] },
    { key: 'fix', name: '页码修正', desc: '校验失败的条目重试定位真实起始页', rows: 8, appended: '章节标题与窗口页文本' },
  ],
  bianxiu: [
    { key: 'system_prompt', name: '维护 agent 系统提示词', desc: '每份文档一次会话的开场指令', rows: 16, vars: ['doc_id', 'title', 'max_calls'] },
  ],
}

const NUMERIC = ['temperature', 'max_tokens', 'retries', 'timeout', 'check_limit', 'verify_sample', 'fix_attempts', 'interval', 'max_calls', 'wall_clock', 'cooldown']

const endpoints = useEndpointsStore()
const loading = ref(true)
const error = ref('')
const saving = ref(false)
const draft = ref(null)
const saved = ref(null)
const defaults = ref(null)
const areas = ref({})

const meta = computed(() => META[props.name])
const promptDefs = computed(() => PROMPTS[props.name])
const dirty = computed(
  () => !!draft.value && !!saved.value && JSON.stringify(draft.value) !== JSON.stringify(saved.value),
)

function clone(value) {
  return JSON.parse(JSON.stringify(value))
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await fetchAgents()
    draft.value = clone(data.agents[props.name])
    saved.value = clone(data.agents[props.name])
    defaults.value = data.defaults
  } catch (err) {
    error.value = err.message || '读取失败'
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  endpoints.load()
  load()
})

function endpointLabel(id) {
  if (!id) return '跟随默认端点'
  const ep = endpoints.endpoints.find((item) => item.id === id)
  return ep ? `${ep.name}（${ep.model}）` : '端点已不存在，保存时回落默认'
}

function promptModified(key) {
  return !!defaults.value && draft.value.prompts[key] !== defaults.value[props.name].prompts[key]
}

function resetPrompt(key) {
  draft.value.prompts[key] = defaults.value[props.name].prompts[key]
}

function insertVar(key, variable) {
  const el = areas.value[key]
  const text = draft.value.prompts[key]
  const start = el?.selectionStart ?? text.length
  const end = el?.selectionEnd ?? start
  const insertion = `{${variable}}`
  draft.value.prompts[key] = text.slice(0, start) + insertion + text.slice(end)
  nextTick(() => {
    el?.focus()
    el?.setSelectionRange(start + insertion.length, start + insertion.length)
  })
}

async function save() {
  saving.value = true
  error.value = ''
  try {
    const payload = clone(draft.value)
    for (const key of NUMERIC) {
      if (key in payload) payload[key] = Number(payload[key])
    }
    const result = await updateAgent(props.name, payload)
    draft.value = clone(result.agent)
    saved.value = clone(result.agent)
  } catch (err) {
    error.value = err.message || '保存失败'
  } finally {
    saving.value = false
  }
}

function discard() {
  draft.value = clone(saved.value)
  error.value = ''
}
</script>

<template>
  <div class="agent-settings">
    <p v-if="loading" class="agent-loading">读取 {{ meta.title }} 设定…</p>
    <p v-else-if="!draft" class="agent-loading">{{ error || '读取失败' }}</p>

    <template v-else>
      <div class="agent-hero">
        <div class="agent-mark">{{ meta.mark }}</div>
        <div class="agent-hero-copy">
          <h2>
            {{ meta.title }}
            <span class="agent-pinyin">{{ meta.pinyin }}</span>
            <span v-if="name === 'gangmu' && !draft.enabled" class="agent-off-badge">已关闭 · 入库走规则解析</span>
          </h2>
          <p>{{ meta.desc }}</p>
          <div class="agent-duties">
            <UiChip v-for="duty in meta.duties" :key="duty" size="sm">{{ duty }}</UiChip>
          </div>
        </div>
        <div class="agent-hero-meta">
          <div v-if="name === 'gangmu'" class="meta-row">
            <span>开关</span>
            <UiSwitch v-model="draft.enabled" aria-label="启用纲目" />
          </div>
          <div class="meta-row">
            <span>端点</span>
            <b>{{ endpointLabel(draft.endpoint) }}</b>
          </div>
        </div>
      </div>

      <div class="agent-grid" :class="{ 'agent-off': name === 'gangmu' && !draft.enabled }">
        <UiPanel title="端点与请求">
          <div class="param-body">
            <label class="param">
              <span class="param-label">端点</span>
              <UiInput v-model="draft.endpoint" type="select">
                <option value="">跟随默认端点</option>
                <option v-for="ep in endpoints.endpoints" :key="ep.id" :value="ep.id">
                  {{ ep.name }}（{{ ep.model }}{{ ep.enabled ? '' : ' · 停用' }}）
                </option>
              </UiInput>
              <span class="param-hint">
                {{ name === 'gangmu'
                  ? 'TOC 抽取要 JSON 稳定，建议绑定便宜快速的模型，不必与编修共用'
                  : '工具调用循环建议绑工具调用强、推理深的模型' }}
              </span>
            </label>

            <label class="param">
              <span class="param-label">temperature</span>
              <span class="param-ctl">
                <input v-model.number="draft.temperature" type="range" min="0" max="1" step="0.05">
                <b class="mono-num">{{ Number(draft.temperature).toFixed(2) }}</b>
              </span>
              <span class="param-hint">{{ name === 'gangmu' ? '结构化抽取建议 0' : '巡检判断可略高于 0，减少机械重复' }}</span>
            </label>

            <div class="param-pair">
              <label class="param">
                <span class="param-label">max_tokens</span>
                <input v-model.number="draft.max_tokens" class="num" type="number" min="256" step="256">
              </label>
              <label class="param">
                <span class="param-label">重试次数</span>
                <input v-model.number="draft.retries" class="num" type="number" min="0" max="5">
              </label>
              <label class="param">
                <span class="param-label">超时（秒）</span>
                <input v-model.number="draft.timeout" class="num" type="number" min="10" max="600" step="10">
              </label>
            </div>
          </div>
        </UiPanel>

        <UiPanel v-if="name === 'gangmu'" title="触发与校验">
          <div class="param-body">
            <label class="param">
              <span class="param-label">目录检测扫描页数</span>
              <input v-model.number="draft.check_limit" class="num" type="number" min="1" max="50">
              <span class="param-hint">只扫描前 N 页；命中后遇到第一个"非目录页"即停止</span>
            </label>
            <label class="param">
              <span class="param-label">页码校验抽样条数</span>
              <input v-model.number="draft.verify_sample" class="num" type="number" min="1" max="100">
              <span class="param-hint">每份文档最多抽 N 条做"标题是否真的在该页"核对</span>
            </label>
            <label class="param">
              <span class="param-label">页码修正尝试次数</span>
              <input v-model.number="draft.fix_attempts" class="num" type="number" min="0" max="5">
              <span class="param-hint">校验失败的条目逐条重试定位的次数上限</span>
            </label>
          </div>
        </UiPanel>

        <UiPanel v-else title="触发与预算">
          <div class="param-body">
            <label class="param">
              <span class="param-label">巡检间隔（小时）</span>
              <input v-model.number="draft.interval" class="num" type="number" min="0" max="720" step="1">
              <span class="param-hint">0 表示不自动巡检，只手动触发；curator 未指定 --every 时使用此值</span>
            </label>
            <div class="param-pair">
              <label class="param has-tip">
                <span class="param-label">每文档工具调用上限<span class="tip-dot" tabindex="0" aria-label="工具调用上限说明">?</span></span>
                <input v-model.number="draft.max_calls" class="num" type="number" min="1" max="100">
                <span class="tip" role="tooltip">每份文档一次巡检里 agent 最多发起的工具调用数：读页、读切片、提交提案各算一次。用完 dispatch 拒绝新调用，agent 直接收尾，已提交的提案保留。</span>
              </label>
              <label class="param has-tip">
                <span class="param-label">墙钟预算（分钟）<span class="tip-dot" tabindex="0" aria-label="墙钟预算说明">?</span></span>
                <input v-model.number="draft.wall_clock" class="num" type="number" min="1" max="600">
                <span class="tip" role="tooltip">一轮巡检从第一份文档到最后一份的真实耗时上限。到点即停，未处理的文档留待下一轮；不是单文档限时，也不是 LLM 调用次数。</span>
              </label>
              <label class="param has-tip">
                <span class="param-label">同类冷却（天）<span class="tip-dot" tabindex="0" aria-label="同类冷却说明">?</span></span>
                <input v-model.number="draft.cooldown" class="num" type="number" min="0" max="90">
                <span class="tip tip-right" role="tooltip">同一文档的同一类问题在冷却期内已处理过（已应用、跳过、人工拒绝或回滚都算处理过），本轮直接跳过。防止反复改写，也让人工还原不会被立刻覆盖。</span>
              </label>
            </div>
            <div class="param">
              <span class="param-label">agent 处理的问题类型</span>
              <div class="kind-checks">
                <label class="kind-check">
                  <input v-model="draft.agent_kinds" type="checkbox" value="unplaced"> 切片未挂接
                </label>
                <label class="kind-check">
                  <input v-model="draft.agent_kinds" type="checkbox" value="duplicate-heading"> 重复标题
                </label>
              </div>
              <span class="param-hint">过短切片、缺失页码走确定性规则，不经 agent；取消勾选后该类问题本轮不处理</span>
            </div>
          </div>
        </UiPanel>

        <UiPanel class="span-2" title="提示词" sub="大括号变量由代码填充；解析失败会回落规则路径，改坏提示词不会写坏数据">
          <div class="prompt-list">
            <div v-for="p in promptDefs" :key="p.key" class="prompt-card">
              <div class="prompt-head">
                <div>
                  <b>{{ p.name }}</b>
                  <span class="prompt-desc">{{ p.desc }}</span>
                </div>
                <div class="prompt-head-ops">
                  <span v-if="promptModified(p.key)" class="mod-badge">已修改</span>
                  <span class="char-count">{{ draft.prompts[p.key].length }} 字符</span>
                  <UiButton size="sm" @click="resetPrompt(p.key)">恢复默认</UiButton>
                </div>
              </div>
              <div v-if="p.vars" class="var-row">
                <span class="var-hint">变量：</span>
                <button
                  v-for="variable in p.vars"
                  :key="variable"
                  type="button"
                  class="var-chip"
                  @click="insertVar(p.key, variable)"
                >{{ '{' + variable + '}' }}</button>
              </div>
              <div v-else class="var-row">
                <span class="var-hint">{{ p.appended }} 追加在提示词末尾</span>
              </div>
              <textarea
                :ref="(el) => { if (el) areas[p.key] = el }"
                v-model="draft.prompts[p.key]"
                class="prompt-editor"
                spellcheck="false"
                :rows="p.rows"
              ></textarea>
            </div>
          </div>
        </UiPanel>
      </div>

      <p v-if="error" class="agent-error">{{ error }}</p>

      <div v-if="dirty" class="dirty-bar">
        <span>{{ meta.title }}有未保存的更改</span>
        <div class="dirty-ops">
          <UiButton :disabled="saving" @click="discard">放弃</UiButton>
          <UiButton variant="primary" :disabled="saving" @click="save">
            {{ saving ? '保存中…' : '保存' }}
          </UiButton>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.agent-settings { display: flex; flex-direction: column; gap: 14px; margin-top: 14px; }
.agent-loading { font-size: 12px; color: var(--faint); }

.agent-hero {
  display: flex; gap: 16px; align-items: flex-start;
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 16px 18px;
}
.agent-mark {
  flex: none; width: 44px; height: 44px; border-radius: 10px;
  background: var(--accent-soft); color: var(--accent);
  display: flex; align-items: center; justify-content: center;
  font-size: 20px; font-weight: 600;
}
.agent-hero-copy { flex: 1; min-width: 0; }
.agent-hero-copy h2 { font-size: 16px; font-weight: 600; letter-spacing: -.01em; }
.agent-pinyin { font-size: 11px; font-weight: 400; color: var(--faint); margin-left: 8px; }
.agent-off-badge {
  font-size: 10.5px; font-weight: 400; margin-left: 10px; padding: 1px 8px;
  border-radius: 8px; background: var(--surface-3); color: var(--muted); vertical-align: 2px;
}
.agent-hero-copy p { font-size: 12px; color: var(--muted); margin-top: 6px; line-height: 1.6; }
.agent-duties { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 10px; }
.agent-hero-meta { flex: none; display: flex; flex-direction: column; gap: 6px; min-width: 180px; }
.meta-row { display: flex; justify-content: space-between; align-items: center; gap: 14px; font-size: 11.5px; }
.meta-row span { color: var(--faint); }
.meta-row b {
  font-weight: 500; color: var(--text); text-align: right; max-width: 220px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}

.agent-grid {
  display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 14px; overflow: visible;
}
.agent-grid .span-2 { grid-column: 1 / -1; }
.agent-grid.agent-off { opacity: .45; pointer-events: none; }

.param-body { display: flex; flex-direction: column; gap: 14px; }
.param { display: flex; flex-direction: column; gap: 5px; }
.param-label { font-size: 12px; font-weight: 500; }
.param-hint { font-size: 10.5px; color: var(--faint); line-height: 1.5; }
.num {
  height: var(--ctl-h); border: 1px solid var(--border); border-radius: var(--ctl-radius);
  background: var(--surface-2); padding: 0 8px; font-size: 12px; outline: none;
  transition: border-color .12s var(--ease), box-shadow .12s var(--ease);
}
.num:hover { border-color: var(--border-strong); }
.num:focus { border-color: var(--accent); box-shadow: var(--focus-ring); }
.param-ctl { display: flex; align-items: center; gap: 10px; }
.param-ctl input[type="range"] { flex: 1; accent-color: var(--accent); }
.mono-num { font-family: var(--mono); font-size: 11.5px; font-weight: 500; min-width: 34px; }
.param-pair { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.kind-checks { display: flex; gap: 14px; flex-wrap: wrap; }
.kind-check { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); cursor: pointer; }
.kind-check input { accent-color: var(--accent); }

/* 白底黑框小字 tooltip（与原型一致） */
.param.has-tip { position: relative; }
.tip-dot {
  display: inline-flex; align-items: center; justify-content: center;
  width: 13px; height: 13px; margin-left: 5px; border-radius: 50%;
  border: 1px solid var(--border-strong); background: var(--surface-2);
  font-size: 9px; color: var(--muted); cursor: help; vertical-align: 1px;
}
.tip {
  display: none; position: absolute; z-index: 30; top: calc(100% + 4px); left: 0;
  width: 250px; padding: 8px 10px;
  background: var(--surface); color: var(--text);
  border: 1px solid var(--text); border-radius: 6px;
  font-size: 10.5px; line-height: 1.6; font-weight: 400;
  box-shadow: 0 4px 14px rgba(0, 0, 0, .12);
  pointer-events: none;
}
.tip-right { left: auto; right: 0; }
.has-tip:hover .tip, .has-tip:focus-within .tip { display: block; }

.prompt-list { display: flex; flex-direction: column; gap: 12px; }
.prompt-card { border: 1px solid var(--border); border-radius: var(--radius); overflow: visible; }
.prompt-head {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 10px 12px; background: var(--surface-2); border-bottom: 1px solid var(--border);
  border-radius: var(--radius) var(--radius) 0 0;
}
.prompt-head b { font-size: 12px; font-weight: 600; }
.prompt-desc { font-size: 10.5px; color: var(--faint); margin-left: 8px; }
.prompt-head-ops { display: flex; align-items: center; gap: 8px; flex: none; }
.mod-badge { font-size: 10px; padding: 1px 7px; border-radius: 8px; background: var(--warn-soft); color: var(--warn); }
.char-count { font-size: 10.5px; color: var(--faint); font-family: var(--mono); }
.var-row { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; padding: 8px 12px 0; }
.var-hint { font-size: 10.5px; color: var(--faint); }
.var-chip {
  border: 1px solid var(--border); background: var(--surface-2); border-radius: 6px;
  font-size: 10.5px; font-family: var(--mono); padding: 1px 7px; color: var(--accent);
}
.var-chip:hover { border-color: var(--accent); }
.prompt-editor {
  display: block; width: calc(100% - 24px); margin: 8px 12px 12px;
  border: 1px solid var(--border); border-radius: var(--ctl-radius); outline: none; resize: vertical;
  background: var(--surface); color: var(--text);
  font-family: var(--mono); font-size: 11.5px; line-height: 1.55;
  padding: 7px 9px; min-height: 60px;
  transition: border-color .12s var(--ease), box-shadow .12s var(--ease);
}
.prompt-editor:hover { border-color: var(--border-strong); }
.prompt-editor:focus { border-color: var(--accent); box-shadow: var(--focus-ring); }

.agent-error { font-size: 11.5px; color: var(--danger); }

.dirty-bar {
  position: sticky; bottom: 12px;
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  background: var(--surface); border: 1px solid var(--border-strong); border-radius: var(--radius);
  padding: 10px 14px; box-shadow: 0 4px 16px rgba(0, 0, 0, .08);
}
.dirty-bar span { font-size: 12px; color: var(--muted); }
.dirty-ops { display: flex; gap: 8px; }

@media (max-width: 900px) {
  .agent-grid { grid-template-columns: 1fr; }
  .agent-hero { flex-direction: column; }
  .agent-hero-meta { min-width: 0; width: 100%; }
  .param-pair { grid-template-columns: 1fr; }
}
</style>