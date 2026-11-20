<script setup>
/**
 * Manage topic collections.
 *
 * The description field is the agent-facing half of this feature: an
 * agent reads it to decide whether a request belongs here. An empty one
 * is therefore flagged in place rather than treated as a harmless blank.
 */
import { ref, shallowRef, watch } from 'vue'
import { createCollection, deleteCollection, updateCollection } from '@/api'
import { useCollectionsStore } from '@/stores/collections'
import { useUiStore } from '@/stores/ui'
import UiButton from '@/components/ui/UiButton.vue'
import UiInput from '@/components/ui/UiInput.vue'

const props = defineProps({
  open: { type: Boolean, default: false },
})
const emit = defineEmits(['close', 'changed'])

const collections = useCollectionsStore()
const ui = useUiStore()

const newName = ref('')
const newDescription = ref('')
const busy = shallowRef(false)
const drafts = ref({}) // id -> { name, description }
const confirming = ref(null) // collection id awaiting delete confirmation

watch(() => props.open, (open) => {
  if (open) {
    collections.refresh()
    resetDrafts()
  }
})

watch(() => collections.items, resetDrafts)

function resetDrafts() {
  drafts.value = Object.fromEntries(
    collections.items.map((item) => [
      item.id,
      { name: item.name, description: item.description || '' },
    ]),
  )
  confirming.value = null
}

async function run(action, successMessage) {
  busy.value = true
  try {
    await action()
    await collections.refresh()
    emit('changed')
    if (successMessage) ui.notify(successMessage)
  } catch (error) {
    ui.notify(error.message)
  } finally {
    busy.value = false
  }
}

function create() {
  const name = newName.value.trim()
  if (!name) return
  run(async () => {
    await createCollection({ name, description: newDescription.value })
    newName.value = ''
    newDescription.value = ''
  }, `已新建分区「${name}」`)
}

function save(item) {
  const draft = drafts.value[item.id] || {}
  run(async () => {
    await updateCollection(item.id, {
      name: draft.name,
      description: draft.description,
    })
  }, '已保存')
}

function remove(item) {
  run(async () => {
    await deleteCollection(item.id)
  }, `已删除分区「${item.name}」，其文档变为未分区`)
  confirming.value = null
}
</script>

<template>
  <Teleport to="body">
    <div v-if="props.open" class="modal-backdrop" @click.self="emit('close')">
      <div class="modal collection-modal">
        <div class="modal-head">
          <div>
            <h2>分区</h2>
            <p>给 agent 看的说明决定它能不能选对区，请写清楚放什么、什么时候用。</p>
          </div>
          <button class="modal-close" @click="emit('close')">×</button>
        </div>

        <div class="create-row">
          <UiInput v-model="newName" size="sm" placeholder="分区名，例如 项目资料" />
          <UiInput
            v-model="newDescription"
            size="sm"
            type="textarea"
            :rows="2"
            placeholder="说明：放什么、什么时候该来这个区…"
          />
          <UiButton size="sm" variant="primary" :disabled="busy || !newName.trim()" @click="create">
            新建
          </UiButton>
        </div>

        <div v-if="collections.loaded && !collections.items.length" class="empty-note">
          还没有分区。先建一个，再把文档归进去。
        </div>

        <div v-for="item in collections.items" :key="item.id" class="collection-row">
          <div class="row-head">
            <UiInput
              v-model="drafts[item.id].name"
              size="sm"
              class="name-input"
              :aria-label="`分区名 ${item.name}`"
            />
            <span class="count">{{ item.docCount }} 份</span>
            <div class="row-actions">
              <UiButton size="sm" :disabled="busy" @click="save(item)">保存</UiButton>
              <UiButton
                v-if="confirming !== item.id"
                size="sm"
                variant="danger"
                :disabled="busy"
                @click="confirming = item.id"
              >删除</UiButton>
            </div>
          </div>
          <UiInput
            v-model="drafts[item.id].description"
            size="sm"
            type="textarea"
            :rows="2"
            placeholder="说明：放什么、什么时候该来这个区…"
          />
          <div v-if="!drafts[item.id].description.trim()" class="missing-note">
            未填写说明 · agent 只能靠分区名判断
          </div>
          <div v-if="confirming === item.id" class="confirm-row">
            <span>分区内 {{ item.docCount }} 份文档会变为未分区，文档本身不会删除。</span>
            <UiButton size="sm" variant="danger" :disabled="busy" @click="remove(item)">确认删除</UiButton>
            <UiButton size="sm" @click="confirming = null">取消</UiButton>
          </div>
        </div>

        <div class="modal-actions">
          <span class="unassigned-note">未分区 {{ collections.unassigned }} 份</span>
          <UiButton size="md" @click="emit('close')">关闭</UiButton>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.collection-modal { width: min(560px, 100%); max-height: min(80vh, 720px); overflow: auto; }
.create-row {
  display: grid;
  grid-template-columns: 180px minmax(0, 1fr) auto;
  gap: 8px;
  align-items: start;
  margin-top: 16px;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--border);
}
.collection-row { padding: 12px 0; border-bottom: 1px solid var(--border); }
.collection-row:last-of-type { border-bottom: 0; }
.row-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.name-input { max-width: 220px; }
.count { font-size: 10.5px; color: var(--faint); }
.row-actions { margin-left: auto; display: flex; gap: 6px; }
.missing-note { margin-top: 5px; font-size: 10.5px; color: var(--warn); }
.confirm-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
  padding: 8px 10px;
  border: 1px solid color-mix(in srgb, var(--danger) 35%, var(--border));
  border-radius: var(--ctl-radius);
  font-size: 11px;
  color: var(--muted);
}
.confirm-row span { flex: 1; }
.empty-note { padding: 18px 0; font-size: 11.5px; color: var(--faint); }
.modal-actions { align-items: center; }
.unassigned-note { margin-right: auto; font-size: 10.5px; color: var(--faint); }
</style>
