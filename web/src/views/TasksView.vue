<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useTasksStore } from '@/stores/quality'
import { fetchTaskDetail } from '@/api'
import { absTime, relTime } from '@/utils/format'
import UiEmpty from '@/components/ui/UiEmpty.vue'
import UiSeg from '@/components/ui/UiSeg.vue'

/**
 * 任务 = 活动层的 tasks 表：跨 agent、跨会话的工作单元。
 *
 * 任务的标题只是索引；它的内容是挂在下面的活动时间线——
 * 谁读过什么、引用过什么、提了哪条勘误、谁复核的、交接了什么。
 * 展开一行才看得到这些，这也正是任务页存在的理由。
 *
 * 和「解析队列」不是一回事：解析队列是文档状态派生出来的入库任务。
 */
const tasksStore = useTasksStore()
const router = useRouter()
const filter = ref('active')

const expandedId = ref(null)
const detail = ref(null)
const detailLoading = ref(false)
const detailError = ref('')

onMounted(() => tasksStore.load())

const STATUS = {
  open: '待办',
  doing: '进行中',
  blocked: '受阻',
  done: '已完成',
  archived: '已归档',
}

const KIND = {
  read: '阅读',
  cite: '引用',
  note: '笔记',
  handoff: '交接',
  erratum: '勘误',
  review: '复核',
  pitfall: '踩坑',
}

const filters = computed(() => [
  { value: 'active', label: '未完成', count: tasksStore.tasks.filter((t) => isActive(t)).length },
  { value: 'done', label: '已完成', count: tasksStore.tasks.filter((t) => t.status === 'done').length },
  { value: 'all', label: '全部', count: tasksStore.tasks.length },
])

function isActive(task) {
  return task.status === 'open' || task.status === 'doing' || task.status === 'blocked'
}

const rows = computed(() => {
  const all = tasksStore.tasks
  if (filter.value === 'active') return all.filter(isActive)
  if (filter.value === 'done') return all.filter((t) => t.status === 'done')
  return all
})

async function toggle(task) {
  if (expandedId.value === task.id) {
    expandedId.value = null
    detail.value = null
    return
  }
  expandedId.value = task.id
  detail.value = null
  detailError.value = ''
  detailLoading.value = true
  try {
    detail.value = await fetchTaskDetail(task.id)
  } catch (err) {
    detailError.value = err.message || '读取失败'
  } finally {
    detailLoading.value = false
  }
}

/** "《文档》 › 章节" when the record is anchored to material. */
function anchorLabel(item) {
  const doc = item.docTitle || ''
  const path = item.headingPath || item.nodeTitle || ''
  if (!doc && !path) return ''
  if (doc && path) return `《${doc}》 › ${path}`
  return doc ? `《${doc}》` : path
}

/** External MCP call traces filed under this task (newest first, capped). */
const detailRuns = computed(() => [...(detail.value?.runs || [])].reverse().slice(0, 6))

const RUN_STATUS = {
  running: '运行中',
  done: '完成',
  failed: '失败',
  cancelled: '已取消',
  queued: '排队中',
}

/** An old session's run often has no run_end; do not show it as live on a
 *  task that is already closed. */
function runState(run) {
  if (run.status === 'running' && detail.value?.task?.status === 'done') return '已结束'
  return RUN_STATUS[run.status] || run.status
}

function openRun(runId) {
  router.push({ path: '/agent', query: { run: runId } })
}
</script>

<template>
  <section class="page">
    <div class="page-head">
      <div>
        <h1>任务</h1>
        <p>agent 的工作单元，跨会话交接。外部 MCP agent 的调用会自动归到「外部会话」任务；展开一行看读过什么、留下了什么。</p>
      </div>
      <UiSeg v-model="filter" :items="filters" />
    </div>

    <div class="panel">
      <div v-if="rows.length" class="task-list">
        <div v-for="task in rows" :key="task.id" class="task-block">
          <div class="task-row" :class="{ open: expandedId === task.id }" @click="toggle(task)">
            <div class="task-top">
              <span class="tstatus" :class="task.status">{{ STATUS[task.status] || task.status }}</span>
              <span v-if="task.kind === 'session'" class="tkind">外部会话</span>
              <span class="task-title">{{ task.title }}</span>
              <span class="task-chevron" :class="{ open: expandedId === task.id }">
                <svg viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg>
              </span>
            </div>
            <div v-if="task.goal" class="task-goal">{{ task.goal }}</div>
            <div class="task-meta">
              <span>{{ task.owner || '未指派' }}</span>
              <span>更新于 {{ relTime(task.updatedAt) }}</span>
            </div>
          </div>

          <div v-if="expandedId === task.id" class="task-detail">
            <div v-if="detailLoading" class="detail-note">读取中…</div>
            <div v-else-if="detailError" class="detail-note danger">{{ detailError }}</div>
            <UiEmpty
              v-else-if="detail && !detail.timeline.length"
              compact
              title="这个任务下还没有记录"
              hint="agent 读资料、提勘误、写交接时会自动挂到当前任务上。"
            />
            <ol v-else-if="detail" class="timeline">
              <li v-for="item in detail.timeline" :key="`${item.source}-${item.id}`" class="tl-item">
                <span class="tl-kind" :class="item.kind">{{ KIND[item.kind] || item.kind }}</span>
                <div class="tl-body">
                  <div class="tl-head">
                    <span class="tl-actor">{{ item.actor || '未署名' }}</span>
                    <span class="tl-time" :title="absTime(item.createdAt)">{{ relTime(item.createdAt) }}</span>
                  </div>
                  <div v-if="item.content" class="tl-text">{{ item.content }}</div>
                  <div v-else-if="item.kind === 'read'" class="tl-text">
                    读了{{ item.headingPath ? `「${item.headingPath}」` : '' }}
                  </div>
                  <div v-else class="tl-text faint">（自动记录，无正文）</div>
                  <div v-if="anchorLabel(item)" class="tl-anchor">{{ anchorLabel(item) }}</div>
                </div>
              </li>
            </ol>
            <div v-if="detail?.runs?.length" class="run-links">
              <div class="runs-head">
                会话记录 {{ detail.counts.runs }} 段
                <span v-if="detailRuns.length < detail.counts.runs">· 最近 {{ detailRuns.length }} 段</span>
              </div>
              <button
                v-for="run in detailRuns"
                :key="run.id"
                type="button"
                class="run-link"
                @click="openRun(run.id)"
              >
                <span class="run-q">{{ run.question }}</span>
                <span class="run-state" :class="run.status">{{ runState(run) }}</span>
                <span class="run-time">{{ relTime(run.createdAt) }}</span>
              </button>
            </div>
            <div v-if="detail" class="detail-foot">
              {{ detail.counts.total }} 条记录 · {{ detail.counts.activities }} 条活动 ·
              {{ detail.counts.notes }} 条手记
            </div>
          </div>
        </div>
      </div>
      <UiEmpty
        v-else
        :title="filter === 'active' ? '没有未完成的任务' : '没有任务'"
        hint="外部 MCP agent 一调用就会自动归入「外部会话」任务；agent 也可以用 task_create() 自己建任务。"
      />
    </div>
  </section>
</template>

<style scoped>
.task-list { padding: 0; }
.task-block { border-bottom: 1px solid var(--border); }
.task-block:last-child { border-bottom: 0; }
.task-row { padding: 12px 16px; cursor: pointer; }
.task-row:hover { background: var(--pill); }
.task-row.open { background: var(--pill); }
.task-top { display: flex; align-items: baseline; gap: 8px; }
.task-title { font-size: 12.5px; font-weight: 500; line-height: 1.45; min-width: 0; flex: 1; }
.task-chevron { flex: none; width: 14px; height: 14px; color: var(--faint); }
.task-chevron svg { width: 14px; height: 14px; fill: none; stroke: currentColor; stroke-width: 2; transition: transform .15s var(--ease); }
.task-chevron.open svg { transform: rotate(180deg); }
.task-goal { font-size: 11.5px; color: var(--muted); margin-top: 4px; line-height: 1.6; }
.task-meta { display: flex; gap: 12px; font-size: 10.5px; color: var(--faint); margin-top: 5px; }
.tstatus {
  font-size: 9.5px;
  padding: 1px 5px;
  border-radius: 3px;
  flex: none;
}
.tstatus.open { color: var(--accent); background: var(--accent-soft); }
.tstatus.doing { color: var(--warn); background: var(--warn-soft); }
.tstatus.blocked { color: var(--danger); background: var(--danger-soft); }
.tstatus.done { color: var(--ok); background: var(--ok-soft); }
.tstatus.archived { color: var(--faint); background: var(--pill); }
.tkind {
  font-size: 9.5px;
  padding: 1px 5px;
  border-radius: 3px;
  flex: none;
  color: var(--muted);
  background: var(--surface-2);
  border: 1px solid var(--border);
}

.task-detail { padding: 0 16px 14px 16px; background: var(--surface-2); }
.detail-note { font-size: 11.5px; color: var(--faint); padding: 10px 0; }
.detail-note.danger { color: var(--danger); }
.timeline { list-style: none; margin: 0; padding: 4px 0 0; }
.tl-item { display: flex; gap: 9px; padding: 7px 0; border-bottom: 1px solid var(--border); }
.tl-item:last-child { border-bottom: 0; }
.tl-kind {
  flex: none;
  font-size: 9.5px;
  padding: 1px 5px;
  border-radius: 3px;
  height: fit-content;
  background: var(--pill);
  color: var(--muted);
}
.tl-kind.read { color: var(--faint); }
.tl-kind.cite { color: var(--accent); background: var(--accent-soft); }
.tl-kind.erratum { color: var(--danger); background: var(--danger-soft); }
.tl-kind.review { color: var(--ok); background: var(--ok-soft); }
.tl-kind.handoff { color: var(--warn); background: var(--warn-soft); }
.tl-kind.pitfall { color: var(--warn); background: var(--warn-soft); }
.tl-body { min-width: 0; flex: 1; }
.tl-head { display: flex; gap: 10px; align-items: baseline; }
.tl-actor { font-size: 11px; font-weight: 500; }
.tl-time { font-size: 10px; color: var(--faint); margin-left: auto; }
.tl-text { font-size: 11.5px; color: var(--muted); line-height: 1.65; margin-top: 2px; white-space: pre-line; }
.tl-text.faint { color: var(--faint); font-style: italic; }
.tl-anchor { font-size: 10px; color: var(--faint); margin-top: 3px; }
.run-links { padding-top: 10px; }
.runs-head { font-size: 10.5px; color: var(--faint); margin-bottom: 4px; }
.run-link {
  display: flex;
  align-items: baseline;
  gap: 8px;
  width: 100%;
  padding: 5px 6px;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.run-link:hover { background: var(--pill); }
.run-q { flex: 1; min-width: 0; font-size: 11.5px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.run-state { flex: none; font-size: 9.5px; color: var(--faint); }
.run-state.done { color: var(--ok); }
.run-state.failed { color: var(--danger); }
.run-state.running { color: var(--accent); }
.run-time { flex: none; font-size: 10px; color: var(--faint); }
.detail-foot { font-size: 10px; color: var(--faint); padding-top: 9px; }
</style>
