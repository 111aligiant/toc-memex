<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useCurationStore, useQualityStore } from '@/stores/quality'
import { useLibraryStore } from '@/stores/library'
import { useWorkspaceStore } from '@/stores/workspace'
import { relTime, statusLabel } from '@/utils/format'
import UiEmpty from '@/components/ui/UiEmpty.vue'
import UiButton from '@/components/ui/UiButton.vue'
import CurationProposals from '@/components/doc/CurationProposals.vue'

const router = useRouter()
const quality = useQualityStore()
const curation = useCurationStore()
const library = useLibraryStore()
const workspace = useWorkspaceStore()

onMounted(() => {
  quality.load()
  curation.load()
  library.load()
})

/**
 * 四个数字全部来自 /api/quality 的 summary，同一个口径。
 * 以前「警告」取 sum(doc.warnings)、「问题清单」取 group count 合计、
 * 「缺失页」前端自己累加，三个数互不相等且无法解释，见审计 B-1。
 */
const stats = computed(() => {
  const s = quality.summary
  if (!s) return []
  const missing = s.byKind['missing-pages'] || 0
  const duplicate = s.byKind['duplicate-heading'] || 0
  const duplicateDoc = s.byKind['duplicate-doc'] || 0
  const unplaced = s.byKind['unplaced'] || 0
  const shortChunks = s.byKind['short-chunks'] || 0
  const structural = s.issues - shortChunks
  return [
    {
      label: '结构问题',
      value: structural,
      meta: `${s.groups - (s.byKind['short-chunks'] ? countKind('short-chunks') : 0)} 类 · 不含短切片提示`,
      tone: structural ? 'warn' : 'ok',
    },
    {
      label: '缺失页',
      value: missing,
      meta: '正文页覆盖缺口',
      tone: missing ? 'danger' : 'ok',
    },
    {
      label: '目录结构',
      value: duplicate + duplicateDoc + unplaced,
      meta: `重复编号 ${duplicate} · 重复文档 ${duplicateDoc} · 未定位 ${unplaced}`,
      tone: duplicate + duplicateDoc + unplaced ? 'warn' : 'ok',
    },
    {
      label: '短切片',
      value: shortChunks,
      meta: '提示性，不阻塞使用',
      tone: '',
    },
  ]
})

/** How many quality groups of one kind (for the stat card subtitle). */
function countKind(kind) {
  return quality.groups.filter((group) => group.kind === kind).length
}

const issueDocs = computed(() => library.issueDocs)

/**
 * 短切片占问题清单的绝大多数（62/76 类），把它们排在后面并折叠起来，
 * 否则结构性问题（缺失页、重复编号）会被淹没。短切片是提示性的，
 * 不是必须处理的问题。
 */
const BULK_KINDS = new Set(['short-chunks'])

const structuralGroups = computed(() =>
  quality.groups.filter((group) => !BULK_KINDS.has(group.kind)),
)

const bulkGroups = computed(() =>
  quality.groups.filter((group) => BULK_KINDS.has(group.kind)),
)

const showBulk = ref(false)

function openDoc(docId) {
  router.push(`/doc/${docId}?tab=quality`)
}
</script>

<template>
  <section class="page">
    <div class="page-head">
      <div>
        <h1>质量</h1>
        <p>跨文档的结构、覆盖和复核问题。</p>
      </div>
    </div>
    <div class="stats">
      <div v-for="stat in stats" :key="stat.label" class="stat" :class="stat.tone">
        <div class="stat-label">{{ stat.label }}</div>
        <div class="stat-value">{{ stat.value }}</div>
        <div class="stat-meta">{{ stat.meta }}</div>
      </div>
    </div>
    <div class="quality-grid">
      <div class="panel">
        <div class="panel-head">
          <h3>问题清单</h3>
          <span class="panel-sub">
            {{ structuralGroups.length }} 类结构问题<template v-if="bulkGroups.length"> · {{ bulkGroups.length }} 类短切片</template>
          </span>
        </div>
        <div class="panel-body">
          <div class="issue-list">
            <div v-for="group in structuralGroups" :key="group.docId + group.title" class="issue-row">
              <span class="issue-sev" :class="group.severity"></span>
              <div class="issue-main">
                <div class="issue-title">{{ group.title }} · {{ group.count }}</div>
                <div class="issue-desc">{{ group.desc }}</div>
              </div>
              <UiButton variant="quiet" size="sm" @click="openDoc(group.docId)">查看</UiButton>
              <UiButton
                v-if="group.relatedDocId"
                variant="quiet"
                size="sm"
                @click="openDoc(group.relatedDocId)"
              >
                另一份
              </UiButton>
            </div>

            <template v-if="bulkGroups.length">
              <button class="bulk-toggle" @click="showBulk = !showBulk">
                <svg viewBox="0 0 24 24" :class="{ open: showBulk }"><path d="m6 9 6 6 6-6"/></svg>
                短切片提示 <span class="bulk-count">{{ bulkGroups.length }} 份文档</span>
              </button>
              <div v-show="showBulk" class="bulk-list">
                <div v-for="group in bulkGroups" :key="group.docId + group.title" class="issue-row">
                  <span class="issue-sev" :class="group.severity"></span>
                  <div class="issue-main">
                    <div class="issue-title">{{ group.title }} · {{ group.count }}</div>
                    <div class="issue-desc">{{ group.desc }}</div>
                  </div>
                  <UiButton variant="quiet" size="sm" @click="openDoc(group.docId)">查看</UiButton>
                </div>
              </div>
            </template>

            <UiEmpty
              v-if="!quality.groups.length"
              title="没有问题"
              hint="全部文档结构校验通过。"
            />
          </div>
        </div>
      </div>
      <div class="panel">
        <div class="panel-head"><h3>有问题的文档</h3></div>
        <div class="table-wrap">
          <table class="data">
            <thead><tr><th>文档</th><th class="num">错误</th><th class="num">警告</th><th>状态</th></tr></thead>
            <tbody>
              <tr v-for="doc in issueDocs" :key="doc.id" @click="openDoc(doc.id)">
                <td><div class="name text-strong">{{ doc.title }}</div><div class="sub text-sub">{{ doc.filename }}</div></td>
                <td class="num">{{ doc.errors }}</td>
                <td class="num">{{ doc.warnings }}</td>
                <td><span class="status" :class="doc.status"><span class="dot"></span>{{ statusLabel(doc.status) }}</span></td>
              </tr>
              <tr v-if="!issueDocs.length"><td colspan="4"><UiEmpty compact title="没有发现问题的文档" /></td></tr>
            </tbody>
          </table>
        </div>
      </div>
      <CurationProposals />
    </div>
  </section>
</template>

<style scoped>
.bulk-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  margin-top: 10px;
  padding: 8px 0;
  background: none;
  border: 0;
  border-top: 1px solid var(--border);
  color: var(--muted);
  font-size: 11.5px;
  text-align: left;
  cursor: pointer;
}
.bulk-toggle:hover { color: var(--text); }
.bulk-toggle svg {
  width: 12px;
  height: 12px;
  fill: none;
  stroke: currentColor;
  stroke-width: 2;
  transition: transform .15s var(--ease);
}
.bulk-toggle svg.open { transform: rotate(180deg); }
.bulk-count { color: var(--faint); }
.bulk-list { opacity: .92; }
</style>
