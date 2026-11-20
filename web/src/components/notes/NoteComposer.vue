<script setup>
import { ref } from 'vue'
import { KIND_LABELS, useNotesStore } from '@/stores/notes'
import { useUiStore } from '@/stores/ui'
import UiButton from '@/components/ui/UiButton.vue'

const props = defineProps({
  anchor: { type: Object, required: true }, // { docId, nodeId, chunkId }
  placeholder: { type: String, default: '留一条手记：做了什么、踩了什么坑…' },
  autofocus: { type: Boolean, default: false },
})

const emit = defineEmits(['posted'])

const notesStore = useNotesStore()
const ui = useUiStore()

const kind = ref('note')
const body = ref('')
const submitting = ref(false)

async function submit() {
  if (!notesStore.author.trim()) {
    ui.notify('先设置你的名字（右侧“我的身份”）')
    return
  }
  if (!body.value.trim()) return
  submitting.value = true
  try {
    await notesStore.post({
      kind: kind.value,
      body: body.value.trim(),
      docId: props.anchor.docId || null,
      nodeId: props.anchor.nodeId || null,
      chunkId: props.anchor.chunkId ?? null,
      replyToId: notesStore.replyTo,
    })
    body.value = ''
    emit('posted')
  } catch (err) {
    ui.notify(`发布失败：${err.message}`)
  } finally {
    submitting.value = false
  }
}

defineExpose({
  prefill(text) {
    body.value = text
  },
  focus() {
    if (props.autofocus) return
    // parent containers scroll to the composer via .note-composer
  },
})
</script>

<template>
  <div class="note-composer">
    <div v-if="notesStore.replyTo" class="replying-bar">
      <span>正在回复 #{{ notesStore.replyTo }}</span>
      <UiButton variant="quiet" size="sm" @click="notesStore.cancelReply()">取消</UiButton>
    </div>
    <div class="composer-row">
      <select v-model="kind" class="composer-kind">
        <option value="note">{{ KIND_LABELS.note }}</option>
        <option value="pitfall">{{ KIND_LABELS.pitfall }}</option>
        <option value="handoff">{{ KIND_LABELS.handoff }}</option>
      </select>
      <UiButton variant="primary" size="sm" :disabled="submitting" @click="submit">发布</UiButton>
    </div>
    <textarea
      v-model="body"
      class="review-note"
      :placeholder="placeholder"
      @keydown.ctrl.enter="submit"
      @keydown.meta.enter="submit"
    ></textarea>
  </div>
</template>

<style scoped>
.composer-row { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.composer-kind { flex: none; }
.replying-bar {
  display: flex; align-items: center; justify-content: space-between;
  font-size: 10.5px; color: var(--accent); background: var(--accent-soft);
  border-radius: var(--radius-sm); padding: 4px 8px; margin-bottom: 6px;
}
.note-composer textarea { margin-top: 2px; }
</style>
