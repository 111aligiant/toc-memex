<script setup>
import { computed, ref } from 'vue'
import TocTree from '@/components/TocTree.vue'
import { useWorkspaceStore } from '@/stores/workspace'
import { useNotesStore } from '@/stores/notes'
import { useUiStore } from '@/stores/ui'
import UiEmpty from '@/components/ui/UiEmpty.vue'
import UiButton from '@/components/ui/UiButton.vue'

const workspace = useWorkspaceStore()
const notesStore = useNotesStore()
const ui = useUiStore()

const emit = defineEmits(['open-notes'])

const detailOpen = ref(true)

const selectedNode = computed(() => workspace.selectedNode)
const nodeNotes = computed(() =>
  selectedNode.value ? notesStore.notesForNode(selectedNode.value.node_id) : [],
)

function selectNode(node) {
  workspace.selectedNodeId = node.node_id
}

function gotoRead(node) {
  if (!workspace.selectNodeAndRead(node)) ui.notify('该节点暂无切片')
}

function gotoChunks(node) {
  if (!node.subtreeChunks) {
    ui.notify('该节点暂无切片')
    return
  }
  workspace.openChunks(node)
}
</script>

<template>
  <div class="toc-layout" :class="{ 'detail-collapsed': !detailOpen }">
    <div class="tree-panel">
      <TocTree
        :nodes="workspace.nodes"
        :expanded="workspace.expanded"
        :selected-id="workspace.selectedNodeId"
        heading="目录结构"
        hint="点击节点查看详情"
        @select="selectNode"
        @toggle="workspace.toggleExpanded"
      />
    </div>
    <aside v-if="detailOpen" class="node-detail">
      <template v-if="selectedNode">
        <h2>{{ selectedNode.title }}</h2>
        <div class="node-path">{{ selectedNode.heading_path }}</div>
        <div class="meta-grid">
          <div class="meta-item"><span>页码</span><b>{{ selectedNode.pageLabel }}</b></div>
          <div class="meta-item"><span>子树切片</span><b>{{ selectedNode.subtreeChunks }}</b></div>
          <div class="meta-item"><span>直接切片</span><b>{{ selectedNode.directChunks }}</b></div>
          <div class="meta-item"><span>层级</span><b>L{{ selectedNode.level }}</b></div>
          <div class="meta-item"><span>来源</span><b>{{ selectedNode.source }}</b></div>
          <div class="meta-item"><span>置信度</span><b>{{ selectedNode.confidence }}</b></div>
        </div>
        <div v-if="selectedNode.issues.length" class="detail-section">
          <div class="detail-section-head"><span class="label">问题</span></div>
          <div class="notice mt-0">{{ selectedNode.issues.join('；') }}</div>
        </div>
        <div v-if="nodeNotes.length" class="detail-section">
          <div class="detail-section-head">
            <span class="label">本节手记 · {{ nodeNotes.length }}</span>
            <UiButton variant="quiet" size="sm" @click="emit('open-notes', { nodeId: selectedNode.node_id })">查看 →</UiButton>
          </div>
          <div class="stat-meta">{{ nodeNotes.length }} 条手记锚定在本节，打开手记 tab 阅读。</div>
        </div>
        <div class="detail-section detail-actions mt-14">
          <UiButton size="sm" @click="gotoRead(selectedNode)">在正文流中查看</UiButton>
          <UiButton size="sm" @click="gotoChunks(selectedNode)">查看切片</UiButton>
        </div>
      </template>
      <UiEmpty v-else compact title="选择左侧节点查看详情" />
    </aside>
  </div>
</template>
