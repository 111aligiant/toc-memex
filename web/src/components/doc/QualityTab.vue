<script setup>
import { computed } from 'vue'
import { useWorkspaceStore } from '@/stores/workspace'

const workspace = useWorkspaceStore()

const cells = computed(() =>
  workspace.coverage.map((row) => ({
    cls: row.nonContent ? 'non' : row.chunks.length ? 'ok' : 'missing',
  })),
)

const checks = computed(() => {
  const stats = workspace.stats
  if (!stats) return []
  const rows = [
    ['无直接切片', stats.nodesWithoutDirect],
    ['叶子无切片', stats.leafNodesWithoutChunks],
    ['未归属切片', stats.orphanChunks],
    ['短切片', stats.shortChunks],
    ['未定位节点', stats.unplacedNodes],
    ['缺失页', stats.missingPages],
  ]
  return rows.map(([label, value]) => ({
    label,
    value,
    tone: value === 0 ? '' : label === '缺失页' || label === '叶子无切片' ? 'danger' : 'warn',
  }))
})

const issues = computed(() => {
  const stats = workspace.stats
  if (!stats) return []
  const out = []
  if (stats.unplacedNodes) {
    out.push({ severity: 'warning', title: '未定位节点', desc: `${stats.unplacedNodes} 个节点没有页码证据，目录中显示为 pages ?。` })
  }
  if (stats.shortChunks) {
    out.push({ severity: 'warning', title: '短切片', desc: `${stats.shortChunks} 个切片少于 50 字，可能是标题或表格片段。` })
  }
  if (stats.orphanChunks) {
    out.push({ severity: 'warning', title: '未归属切片', desc: `${stats.orphanChunks} 个切片没有挂到目录节点。` })
  }
  if (stats.nodesWithoutDirect) {
    out.push({ severity: 'warning', title: '无直接切片节点', desc: `${stats.nodesWithoutDirect} 个节点没有直接切片，正文落在子节点，通过子树聚合导航。` })
  }
  if (stats.missingPages) {
    out.push({ severity: 'warning', title: '缺失页', desc: `正文覆盖 ${stats.coveredPages}/${stats.contentPages}，${stats.missingPages} 页没有切片。` })
  }
  return out
})
</script>

<template>
  <div class="quality-grid">
    <div class="panel">
      <div class="panel-head">
        <h3>页面覆盖</h3>
        <span class="panel-sub">{{ workspace.stats?.coveredPages }} / {{ workspace.stats?.contentPages }} 正文页</span>
      </div>
      <div class="panel-body">
        <div class="coverage-strip">
          <span v-for="(cell, index) in cells" :key="index" class="page-cell" :class="cell.cls"></span>
        </div>
        <div class="coverage-legend">
          <span class="legend-item"><span class="legend-dot ok"></span>有切片</span>
          <span class="legend-item"><span class="legend-dot missing"></span>缺失页</span>
          <span class="legend-item"><span class="legend-dot cover"></span>封面 / 目录</span>
        </div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-head">
        <h3>质量检查</h3>
        <span class="panel-sub">{{ issues.length ? issues.length + ' 类问题' : '结构校验通过' }}</span>
      </div>
      <div class="panel-body">
        <div class="check-grid">
          <div v-for="check in checks" :key="check.label" class="check-tile" :class="check.tone">
            <b>{{ check.value }}</b><span>{{ check.label }}</span>
          </div>
        </div>
        <div v-if="issues.length" class="issue-list mt-14">
          <div v-for="issue in issues" :key="issue.title" class="issue-row">
            <span class="issue-sev" :class="issue.severity"></span>
            <div class="issue-main">
              <div class="issue-title">{{ issue.title }}</div>
              <div class="issue-desc">{{ issue.desc }}</div>
            </div>
          </div>
        </div>
        <div v-else class="quality-ok">结构校验通过，没有发现目录或切片问题。</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.quality-ok { padding: 18px 0; text-align: left; color: var(--ok); font-size: 12px; }
</style>
