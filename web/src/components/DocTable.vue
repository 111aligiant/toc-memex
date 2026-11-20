<script setup>
import { computed } from 'vue'
import { coveragePercent, coverageText, coverageTone, fileClass, fileExt, relTime, statusLabel } from '@/utils/format'
import IngestNote from '@/components/notes/IngestNote.vue'
import UiButton from '@/components/ui/UiButton.vue'

const props = defineProps({
  docs: { type: Array, required: true },
  variant: { type: String, default: 'full' }, // 'full' | 'compact'
  selectedIds: { type: Array, default: () => [] },
  // Library-only: a trailing 删除 action. Bench reuses this table to pick
  // documents, where a delete button would be destructive noise.
  deletable: { type: Boolean, default: false },
})

const emit = defineEmits(['open', 'toggle-select', 'delete'])

const showSelect = computed(() => props.variant === 'full')
const showNodes = computed(() => props.variant === 'full')
const showIssues = computed(() => props.variant === 'full')
const showUpdated = computed(() => props.variant === 'full')
const showProvenance = computed(() => props.variant === 'full')
const showCollection = computed(() => props.variant === 'full')

function provenanceLabel(doc) {
  const p = doc.provenance
  if (!p || p.ocr_used == null) return '未记录'
  return p.ocr_label || (p.ocr_used ? 'OCR' : '文本层')
}

function provenanceTitle(doc) {
  const p = doc.provenance || {}
  const bits = []
  if (p.ocr_used === false) bits.push('未调用 OCR')
  if (p.file_exists === false) bits.push('原始文件缺失')
  if (p.llm_outcome) bits.push(`LLM：${p.llm_outcome}`)
  if (p.recorded_by) bits.push(`来源：${p.recorded_by}`)
  return bits.join(' · ')
}

function isSelected(doc) {
  return props.selectedIds.includes(doc.id)
}
</script>

<template>
  <div class="table-wrap">
    <table class="data">
      <thead>
        <tr>
          <th v-if="showSelect" class="col-check"></th>
          <th>文档</th>
          <th v-if="showCollection">分区</th>
          <th class="num">页</th>
          <th v-if="showNodes" class="num">节点</th>
          <th class="num">切片</th>
          <th>覆盖</th>
          <th v-if="showProvenance">入库来源</th>
          <th v-if="showIssues">问题</th>
          <th>状态</th>
          <th v-if="showUpdated">更新</th>
          <th v-if="props.deletable"></th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="doc in docs"
          :key="doc.id"
          :class="{ selected: isSelected(doc) }"
          @click="emit('open', doc)"
        >
          <td v-if="showSelect" @click.stop>
            <input
              type="checkbox"
              :checked="isSelected(doc)"
              @change="emit('toggle-select', doc.id)"
            >
          </td>
          <td>
            <div class="cell-title">
              <div class="file-icon" :class="fileClass(doc)">{{ fileExt(doc) }}</div>
              <div>
                <div class="name">{{ doc.title }}</div>
                <div class="sub">{{ doc.filename }}</div>
                <IngestNote :doc="doc" />
              </div>
            </div>
          </td>
          <td v-if="showCollection" class="collection-cell">
            <span :class="{ faint: !doc.collectionName }">{{ doc.collectionName || '—' }}</span>
          </td>
          <td class="num">{{ doc.pages }}</td>
          <td v-if="showNodes" class="num">{{ doc.nodes }}</td>
          <td class="num">{{ doc.chunks }}</td>
          <td>
            <div class="coverage-cell">
              <div class="coverage-bar">
                <div class="coverage-fill" :class="coverageTone(doc)" :style="{ width: coveragePercent(doc) + '%' }"></div>
              </div>
              <span class="coverage-text">{{ coverageText(doc) }}</span>
            </div>
          </td>
          <td v-if="showProvenance">
            <span
              class="prov-label"
              :class="{ missing: doc.provenance?.file_exists === false }"
              :title="provenanceTitle(doc)"
            >{{ provenanceLabel(doc) }}</span>
          </td>
          <td v-if="showIssues">
            <span class="issue-count">
              <span v-if="doc.errors" class="danger">{{ doc.errors }} 错误</span>
              <span v-if="doc.warnings" class="warn">{{ doc.warnings }} 警告</span>
              <span v-if="!doc.errors && !doc.warnings" class="faint">—</span>
            </span>
          </td>
          <td><span class="status" :class="doc.status"><span class="dot"></span>{{ statusLabel(doc.status) }}</span></td>
          <td v-if="showUpdated" class="faint">{{ relTime(doc.updatedAt) }}</td>
          <td v-if="props.deletable" class="row-action" @click.stop>
            <UiButton variant="danger" size="sm" @click="emit('delete', doc)">删除</UiButton>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.prov-label { font-size: 11px; color: var(--muted); white-space: nowrap; }
.collection-cell { font-size: 11px; color: var(--muted); white-space: nowrap; }
.prov-label.missing { color: var(--danger); }
.row-action { width: 1%; white-space: nowrap; text-align: right; }
</style>
