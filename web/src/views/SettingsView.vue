<script setup>
import { onMounted, ref, shallowRef } from 'vue'
import { useRouter } from 'vue-router'
import { fetchSettings, updateLlmLogMode } from '@/api'
import { useLibraryStore } from '@/stores/library'
import { useUiStore } from '@/stores/ui'
import AgentSettings from '@/components/AgentSettings.vue'
import EndpointPanel from '@/components/EndpointPanel.vue'
import OcrPanel from '@/components/OcrPanel.vue'
import UiButton from '@/components/ui/UiButton.vue'
import UiPanel from '@/components/ui/UiPanel.vue'
import UiSeg from '@/components/ui/UiSeg.vue'

const library = useLibraryStore()
const ui = useUiStore()
const router = useRouter()

const TABS = [
  { value: 'general', label: '通用' },
  { value: 'endpoints', label: '模型端点' },
  { value: 'gangmu', label: '纲目 agent' },
  { value: 'bianxiu', label: '编修 agent' },
]
const tab = ref('general')

// 切片参数由服务端的 Settings 决定，界面只展示生效值，不伪造可编辑的假象。
const settings = shallowRef(null)
const settingsError = shallowRef('')

const LOG_MODES = [
  { value: 'full', label: 'full' },
  { value: 'meta', label: 'meta' },
  { value: 'off', label: 'off' },
]
const logMode = ref('full')
const logSaving = ref(false)
const logError = ref('')

onMounted(async () => {
  library.load()
  try {
    settings.value = await fetchSettings()
    logMode.value = settings.value.llm?.logMode || 'full'
  } catch (err) {
    settingsError.value = err.message
  }
})

async function changeLogMode(mode) {
  const previous = logMode.value
  logMode.value = mode
  logSaving.value = true
  logError.value = ''
  try {
    await updateLlmLogMode(mode)
  } catch (err) {
    logMode.value = previous
    logError.value = err.message || '保存失败'
  } finally {
    logSaving.value = false
  }
}
</script>

<template>
  <section class="page">
    <div class="page-head">
      <div>
        <h1>设置</h1>
        <p>模型端点、智能体（纲目 / 编修）、OCR 与切片参数。</p>
      </div>
    </div>

    <UiSeg v-model="tab" :items="TABS" aria-label="设置分类" class="settings-subnav" />

    <!-- ── 通用 ── -->
    <div v-if="tab === 'general'" class="settings-grid">
      <OcrPanel />
      <UiPanel title="切片" sub="只读 · 由 .env 决定">
        <template v-if="settings">
          <div class="setting-row">
            <span class="label">最大字符数</span>
            <span class="value">{{ settings.chunk.maxChars }}</span>
          </div>
          <div class="setting-row">
            <span class="label">重叠字符</span>
            <span class="value">{{ settings.chunk.overlap }}</span>
          </div>
          <div class="setting-row">
            <span class="label">封面 / 目录</span>
            <span class="value">不切片</span>
          </div>
          <div class="stat-meta" style="margin-top: 10px;">
            改这些值需要编辑 <code>.env</code> 里的
            <code>TOCMEMEX_MAX_CHUNK_CHARS</code> / <code>TOCMEMEX_CHUNK_OVERLAP</code> 后重新入库。
          </div>
        </template>
        <div v-else-if="settingsError" class="stat-meta">读取失败：{{ settingsError }}</div>
        <div v-else class="stat-meta">读取中…</div>
      </UiPanel>

      <UiPanel title="LLM 调用日志" sub="记录纲目与编修的每次调用">
        <div class="setting-row">
          <div>
            <div class="label">日志粒度</div>
            <div class="desc">full 存 800 字符预览；meta 只存长度；off 关闭</div>
          </div>
          <UiSeg
            :model-value="logMode"
            :items="LOG_MODES"
            size="sm"
            aria-label="日志粒度"
            @update:model-value="changeLogMode"
          />
        </div>
        <div class="setting-row">
          <div>
            <div class="label">调用记录</div>
            <div class="desc">在「LLM 用量」页查看每次调用的耗时、token 与导出</div>
          </div>
          <UiButton size="sm" @click="router.push('/llm')">查看</UiButton>
        </div>
        <div v-if="logError" class="stat-meta log-error">{{ logError }}</div>
        <div v-else-if="logSaving" class="stat-meta">保存中…</div>
      </UiPanel>

      <UiPanel title="界面">
        <div class="setting-row">
          <div><div class="label">主题</div><div class="desc">浅色 / 深色</div></div>
          <UiButton size="sm" @click="ui.toggleTheme">
            {{ ui.theme === 'light' ? '切换到深色' : '切换到浅色' }}
          </UiButton>
        </div>
        <div class="setting-row">
          <div><div class="label">实现</div><div class="desc">Toc Memex · Vue 3 + Vite，数据来自只读 API</div></div>
          <span class="value">v0.1</span>
        </div>
      </UiPanel>
    </div>

    <!-- ── 模型端点 ── -->
    <div v-else-if="tab === 'endpoints'" class="settings-single">
      <EndpointPanel />
      <p class="panel-note">
        纲目 / 编修可在各自页面单独绑定端点；未绑定时使用这里的默认端点。
        解析规则不变：DB 默认 → <code>.env</code> 回落，都没有时 LLM 功能关闭、入库走规则路径。
      </p>
    </div>

    <!-- ── 纲目 / 编修 ── -->
    <AgentSettings v-else-if="tab === 'gangmu'" name="gangmu" />
    <AgentSettings v-else-if="tab === 'bianxiu'" name="bianxiu" />
  </section>
</template>

<style scoped>
.settings-subnav { margin-top: 14px; }
.settings-single { margin-top: 14px; display: flex; flex-direction: column; gap: 14px; }
.panel-note { font-size: 10.5px; color: var(--faint); line-height: 1.6; padding: 0 2px; }
.panel-note code {
  font-family: var(--mono); font-size: 10.5px;
  background: var(--surface-2); border-radius: 3px; padding: 0 4px;
}
.log-error { color: var(--danger); }
.setting-row {
  display: flex; align-items: center; justify-content: space-between;
  gap: 16px; padding: 11px 0; border-bottom: 1px solid var(--border);
}
.setting-row:last-of-type { border-bottom: 0; }
.setting-row .label { font-size: 12px; }
.setting-row .desc { font-size: 10.5px; color: var(--faint); margin-top: 2px; }
.setting-row .value { font-size: 11.5px; color: var(--muted); font-family: var(--mono); }
</style>