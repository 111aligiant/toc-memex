<script setup>
import { computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useCurationStore } from '@/stores/quality'
import { absTime } from '@/utils/format'
import UiButton from '@/components/ui/UiButton.vue'
import UiChip from '@/components/ui/UiChip.vue'
import UiEmpty from '@/components/ui/UiEmpty.vue'

/**
 * L2 待批：curator 的高风险改动（改页码、合并/拆分目录节点）只落提案，
 * 等人批准。放在质量页问题清单右侧，不新开视图（ADR-0002 §16.1 决议 3）。
 */
const curation = useCurationStore()
const router = useRouter()

onMounted(() => curation.load())

const OPS = {
  set_page: '改页码',
  merge_nodes: '合并目录节点',
  split_node: '拆分目录节点',
  merge_chunks: '合并切片',
  reassign_chunk: '切片改挂节点',
  drop_chunk: '删除碎片切片',
  split_chunk: '拆分切片',
  set_summary: '改摘要',
  set_keywords: '改关键词',
  correct_text: '字符级更正',
  no_op: '无需改动',
}

const KIND = {
  'short-chunks': '短切片',
  'missing-pages': '缺失页',
  unplaced: '未定位节点',
  'duplicate-heading': '重复编号',
  agent: 'agent 维护',
}

const STRATEGY = {
  rule_merge: '规则合并',
  rule_page_ranges: '规则页码',
  agent_chunk: 'agent 切片',
  agent_toc: 'agent 目录',
}

function opLabel(op) {
  return OPS[op] || op
}

const rows = computed(() =>
  curation.proposals.map((proposal) => {
    const pending = proposal.results.filter((item) => item.status === 'pending')
    return {
      ...proposal,
      pending,
      pendingCount: pending.length,
      summary: pending
        .map((item) => opLabel(item.op))
        .filter((label, index, all) => all.indexOf(label) === index)
        .join(' · '),
    }
  }),
)

function detail(row) {
  return row.pending
    .map((item) => {
      const index = item.index
      const op = row.ops[index] || {}
      if (op.op === 'set_page') {
        return `第 ${op.page_start + 1}–${op.page_end + 1} 页 · 证据「${op.evidence || '—'}」`
      }
      return op.evidence || op.op
    })
    .join('\n')
}

async function approve(row) {
  if (!window.confirm(`批准这项改动？\n\n${detail(row)}`)) return
  await curation.approve(row.id)
}

async function reject(row) {
  if (!window.confirm('拒绝这项提案？不会应用任何改动。')) return
  await curation.reject(row.id)
}
</script>

<template>
  <div class="panel">
    <div class="panel-head">
      <h3>待批改动</h3>
      <span class="panel-sub">
        {{ curation.proposals.length ? curation.proposals.length + ' 项' : '无' }}
      </span>
    </div>
    <div class="panel-body">
      <div v-if="curation.loading" class="proposal-note">读取中…</div>
      <div v-else-if="curation.error" class="proposal-note danger">{{ curation.error }}</div>
      <div v-else-if="!rows.length" class="proposal-note">
        没有需要人工批准的改动。低风险改动由 curator 自动应用，可在文档「信息」页回退。
      </div>
      <div v-for="row in rows" :key="row.id" class="proposal-row">
        <div class="proposal-head">
          <span class="proposal-kind">{{ KIND[row.kind] || row.kind || '维护' }}</span>
          <UiChip tone="warn" size="sm">{{ row.pendingCount }} 项待批</UiChip>
          <span class="proposal-when">{{ absTime(row.createdAt) }}</span>
        </div>
        <div class="proposal-ops">{{ row.summary }}</div>
        <div v-if="row.rationale" class="proposal-why">{{ row.rationale }}</div>
        <div class="proposal-detail">{{ detail(row) }}</div>
        <div class="proposal-actions">
          <UiButton
            variant="primary"
            size="sm"
            :disabled="curation.busyId === row.id"
            @click="approve(row)"
          >
            批准
          </UiButton>
          <UiButton
            variant="quiet"
            size="sm"
            :disabled="curation.busyId === row.id"
            @click="reject(row)"
          >
            拒绝
          </UiButton>
          <UiButton
            v-if="row.docId"
            variant="ghost"
            size="sm"
            @click="router.push(`/doc/${row.docId}?tab=toc`)"
          >
            查看文档
          </UiButton>
          <span class="proposal-strategy">{{ STRATEGY[row.strategy] || row.strategy }}</span>
        </div>
      </div>
      <UiEmpty
        v-if="!curation.loading && !curation.error && !rows.length"
        compact
        title=""
      />
    </div>
  </div>
</template>

<style scoped>
.proposal-note { font-size: 11.5px; color: var(--faint); line-height: 1.7; padding: 4px 0; }
.proposal-note.danger { color: var(--danger); }
.proposal-row { padding: 10px 0; border-bottom: 1px solid var(--border); }
.proposal-row:last-child { border-bottom: 0; }
.proposal-head { display: flex; align-items: center; gap: 8px; }
.proposal-kind { font-size: 12px; font-weight: 600; }
.proposal-when { margin-left: auto; font-size: 10.5px; color: var(--faint); }
.proposal-ops { font-size: 11.5px; color: var(--muted); margin-top: 4px; }
.proposal-why { font-size: 11px; color: var(--faint); margin-top: 3px; }
.proposal-detail {
  font-size: 10.5px;
  color: var(--faint);
  margin-top: 5px;
  white-space: pre-line;
  line-height: 1.6;
}
.proposal-actions { display: flex; align-items: center; gap: 6px; margin-top: 8px; }
.proposal-strategy { margin-left: auto; font-size: 10px; color: var(--faint); }
</style>
