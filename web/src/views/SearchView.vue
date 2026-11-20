<script setup>
import { onMounted, ref, shallowRef, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { search } from '@/api'
import { useCollectionsStore } from '@/stores/collections'
import { useUiStore } from '@/stores/ui'
import { fileClass, fileExt, safeSnippet } from '@/utils/format'
import UiChip from '@/components/ui/UiChip.vue'
import UiEmpty from '@/components/ui/UiEmpty.vue'
import UiButton from '@/components/ui/UiButton.vue'

const route = useRoute()
const router = useRouter()
const collections = useCollectionsStore()
const ui = useUiStore()

const input = ref('')
const query = ref('')
const results = shallowRef([])
const loading = shallowRef(false)
const searched = shallowRef(false)
const collection = ref('')
const hint = ref('')

// 固定的示例检索词，不是「最近搜索」——没有任何记录逻辑。
const suggestedQueries = ['概述', '方法', '附录', '结论']

async function run(value) {
  const q = (value || input.value).trim()
  if (!q) return
  input.value = q
  query.value = q
  loading.value = true
  try {
    const data = await search(q, 20, collection.value)
    results.value = data.results
    hint.value = data.hint || ''
    searched.value = true
    if (route.query.q !== q) router.replace({ query: { ...route.query, q } })
  } catch (error) {
    ui.notify(error.message)
  } finally {
    loading.value = false
  }
}

async function clearCollection() {
  collection.value = ''
  if (query.value) await run(query.value)
}

onMounted(() => collections.load())

watch(collection, () => {
  if (query.value) run(query.value)
})

watch(() => route.query.q, (value) => {
  if (value && value !== query.value) run(value)
}, { immediate: true })

onMounted(() => {
  if (route.query.q) input.value = route.query.q
})

function openResult(result) {
  if (result.matchType === 'doc') {
    router.push({ path: `/doc/${result.docId}`, query: { tab: 'chunks' } })
    return
  }
  router.push({
    path: `/doc/${result.docId}`,
    query: { tab: 'chunks', ...(result.chunkId != null ? { chunk: result.chunkId } : {}) },
  })
}

const MATCH_LABELS = { bm25: '正文', section: '章节', title: '标题', like: '匹配', doc: '整份文档' }

function matchLabel(result) {
  return MATCH_LABELS[result.matchType] || result.matchType || ''
}

// normalize bm25 scores (negative, smaller=better) into a 0..1 relevance bar
function scoreLabel(result) {
  if (result.matchType === 'doc') return ''
  if (result.matchType !== 'bm25') return result.score?.toFixed ? result.score.toFixed(2) : result.score
  const values = results.value.filter((item) => item.matchType === 'bm25').map((item) => item.score)
  const min = Math.min(...values, 0)
  const max = Math.max(...values, -0.0001)
  if (min === max) return '1.00'
  const normalized = 1 - (result.score - min) / (max - min) * 0.5
  return normalized.toFixed(2)
}
</script>

<template>
  <section class="page">
    <div class="page-head">
      <div>
        <h1>搜索</h1>
        <p>跨文档检索切片，结果可直接跳转到对应文档。</p>
      </div>
    </div>
    <div class="search-hero">
      <div class="search-input-lg">
        <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
        <input v-model="input" type="search" placeholder="输入关键词，例如：概述 / 方法 / 结论" @keyup.enter="run()">
      </div>
      <select v-model="collection" class="search-collection" aria-label="搜索范围：分区">
        <option value="">全部分区</option>
        <option value="unassigned">未分区</option>
        <option v-for="item in collections.items" :key="item.id" :value="item.id">
          {{ item.name }}
        </option>
      </select>
      <UiButton variant="primary" size="md" @click="run()">搜索</UiButton>
    </div>
    <div class="recent-searches">
      <button v-for="item in suggestedQueries" :key="item" class="chip" @click="run(item)">{{ item }}</button>
    </div>
    <div class="panel">
      <div class="panel-head">
        <h3>{{ loading ? '搜索中…' : searched ? `${results.length} 条结果` : '搜索结果' }}</h3>
        <span v-if="query" class="panel-sub">“{{ query }}”</span>
      </div>
      <div v-if="hint" class="scope-hint">
        <span>{{ hint }}</span>
        <UiButton size="sm" @click="clearCollection">在全部文档中搜索</UiButton>
      </div>
      <div class="result-list">
        <div v-for="result in results" :key="result.chunkId" class="result-row" @click="openResult(result)">
          <div class="file-icon result-icon" :class="fileClass({ type: 'pdf' })">{{ fileExt({ type: 'pdf' }) }}</div>
          <div class="result-main">
            <div class="result-title">
              {{ result.doc }}<template v-if="result.path"> · {{ result.path }}</template>
              <UiChip size="sm">{{ result.collection?.name || '未分区' }}</UiChip>
            </div>
            <div class="result-path"><template v-if="result.pageStart != null">P{{ result.pageStart + 1 }}</template><template v-if="result.pageStart != null && matchLabel(result)"> · </template>{{ matchLabel(result) }}</div>
            <!-- eslint-disable-next-line vue/no-v-html -->
            <div class="result-snippet" v-html="safeSnippet(result.snippet)"></div>
          </div>
          <div class="result-meta">
            <span class="result-score">{{ scoreLabel(result) }}</span>
            <span>→</span>
          </div>
        </div>
        <UiEmpty
          v-if="!results.length && searched && !loading"
          title="没有匹配结果"
          hint="换个关键词，或放宽筛选。"
        />
        <UiEmpty
          v-if="!searched && !loading"
          title="输入关键词开始搜索"
          hint="支持章节号（8.3）、文档编号（如 ABC-123）和正文关键词。"
        />
      </div>
    </div>
  </section>
</template>

<style scoped>
.search-collection {
  height: 38px;
  padding: 0 10px;
  border: 1px solid var(--border);
  border-radius: var(--ctl-radius);
  background: var(--surface);
  color: var(--text);
  font-size: 12px;
}
.search-collection:focus { border-color: var(--accent); box-shadow: var(--focus-ring); }
.scope-hint {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 14px;
  border-bottom: 1px solid var(--border);
  background: var(--surface-2);
  font-size: 11.5px;
  color: var(--muted);
  line-height: 1.6;
}
.scope-hint span { flex: 1; }
.result-title :deep(.ui-chip) { margin-left: 6px; vertical-align: 1px; }
</style>
