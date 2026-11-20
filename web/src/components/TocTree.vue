<script setup>
import { computed } from 'vue'

const props = defineProps({
  nodes: { type: Array, required: true }, // flat, ordered; uses parent_id + expanded ids
  expanded: { type: Array, required: true },
  selectedId: { type: String, default: null },
  heading: { type: String, default: '目录' },
  hint: { type: String, default: '' },
})

const emit = defineEmits(['select', 'toggle'])

const visible = computed(() => {
  const out = []
  const byParent = new Map()
  for (const node of props.nodes) {
    const parent = node.parent_id
    if (!byParent.has(parent)) byParent.set(parent, [])
    byParent.get(parent).push(node)
  }
  const rootParentIds = new Set(props.nodes.map((node) => node.parent_id))
  const walk = (node, depth) => {
    out.push({ node, depth })
    if (props.expanded.includes(node.node_id)) {
      for (const child of byParent.get(node.node_id) || []) walk(child, depth + 1)
    }
  }
  for (const node of props.nodes) {
    if (!node.parent_id || !rootParentIds.has(node.parent_id)) walk(node, 0)
  }
  return out
})

function hasChildren(node) {
  return props.nodes.some((item) => item.parent_id === node.node_id)
}
</script>

<template>
  <div class="tree-panel">
    <div class="tree-head">
      <h3>{{ heading }}</h3>
      <span v-if="hint" class="tree-count">{{ hint }}</span>
    </div>
    <div class="tree-body">
      <div
        v-for="{ node, depth } in visible"
        :key="node.node_id"
        class="tree-row"
        :class="{ selected: node.node_id === selectedId }"
        :style="{ paddingLeft: 7 + depth * 15 + 'px' }"
        @click="emit('select', node)"
      >
        <button
          v-if="hasChildren(node)"
          class="tree-caret"
          :class="{ open: expanded.includes(node.node_id) }"
          @click.stop="emit('toggle', node.node_id)"
        >
          <svg viewBox="0 0 24 24"><path d="m9 6 6 6-6 6"/></svg>
        </button>
        <span v-else class="tree-caret placeholder"></span>
        <span class="tree-title">{{ node.title }}</span>
        <span v-if="node.issue" class="tree-issue" :class="node.issue"></span>
        <span class="tree-meta">{{ node.pageLabel }}</span>
      </div>
    </div>
  </div>
</template>
