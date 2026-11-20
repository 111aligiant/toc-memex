<script setup>
/**
 * 入库备注：上传时由人填写、每份文档一条。
 *
 * 人类在知识库页可以直接改，手记页的「入库备注」分类里也能改；agent 只能
 * 通过 MCP 读取（list_notes / get_document），没有任何写入工具。组件自己
 * 调用保存接口并维护本地值，因为知识库的 documents 是 shallowRef，改 prop
 * 对象不会自动触发渲染。
 */
import { nextTick, ref } from 'vue'
import { saveIngestNote } from '@/api'
import { useUiStore } from '@/stores/ui'

const props = defineProps({
  doc: { type: Object, required: true },
})
const emit = defineEmits(['saved'])
const ui = useUiStore()

const body = ref(props.doc.ingestNote || '')
const editing = ref(false)
const draft = ref('')
const saving = ref(false)
const input = ref(null)

function start() {
  draft.value = body.value
  editing.value = true
  nextTick(() => input.value?.focus())
}

function cancel() {
  editing.value = false
}

async function save() {
  if (saving.value) return
  saving.value = true
  try {
    const data = await saveIngestNote(props.doc.id, draft.value)
    body.value = data.note?.body || ''
    // Keep the list's copy in sync for the next render/filter pass.
    props.doc.ingestNote = body.value
    props.doc.ingestNoteId = data.note?.id || null
    editing.value = false
    emit('saved', body.value)
    ui.notify(body.value ? '入库备注已保存' : '入库备注已清除')
  } catch (error) {
    ui.notify(error.message)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="ingest-note" @click.stop>
    <template v-if="editing">
      <textarea
        ref="input"
        v-model="draft"
        rows="2"
        maxlength="20000"
        class="in-input"
        placeholder="这份文件是什么、从哪来、打算怎么用…"
        @keydown.ctrl.enter.prevent="save"
        @keydown.meta.enter.prevent="save"
        @keydown.esc.prevent="cancel"
      ></textarea>
      <div class="in-actions">
        <button class="in-btn primary" :disabled="saving" @click="save">
          {{ saving ? '保存中…' : '保存' }}
        </button>
        <button class="in-btn" :disabled="saving" @click="cancel">取消</button>
        <span class="in-hint">Ctrl+Enter 保存 · 清空后保存即删除</span>
      </div>
    </template>
    <template v-else>
      <span class="in-label">入库备注</span>
      <span v-if="body" class="in-body" :title="body" @click="start">{{ body }}</span>
      <button v-else class="in-add" @click="start">＋添加</button>
    </template>
  </div>
</template>

<style scoped>
.ingest-note {
  display: flex;
  align-items: baseline;
  gap: 6px;
  margin-top: 3px;
  flex-wrap: wrap;
  min-width: 0;
}
.in-label {
  flex: none;
  font-size: 9.5px;
  color: var(--muted);
  background: var(--pill);
  border-radius: 3px;
  padding: 0 4px;
}
.in-body {
  font-size: 10.5px;
  color: var(--muted);
  line-height: 1.5;
  cursor: text;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  word-break: break-word;
}
.in-body:hover { color: var(--text); }
.in-add {
  border: 0;
  background: none;
  padding: 0;
  font-size: 10.5px;
  color: var(--faint);
  cursor: pointer;
}
.in-add:hover { color: var(--accent); }
.in-input {
  width: 100%;
  box-sizing: border-box;
  resize: vertical;
  min-height: 46px;
  margin-top: 2px;
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: var(--ctl-radius);
  background: var(--surface);
  color: var(--text);
  font: inherit;
  font-size: 11.5px;
  line-height: 1.55;
}
.in-input:focus { outline: none; border-color: var(--accent); box-shadow: var(--focus-ring); }
.in-actions { display: flex; align-items: center; gap: 8px; margin-top: 4px; }
.in-btn {
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text);
  border-radius: 5px;
  padding: 2px 9px;
  font-size: 10.5px;
  cursor: pointer;
}
.in-btn.primary { border-color: var(--accent); color: var(--accent); }
.in-btn:disabled { opacity: .55; cursor: default; }
.in-hint { font-size: 9.5px; color: var(--faint); }
</style>