<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useJobsStore } from '@/stores/quality'
import { useUiStore } from '@/stores/ui'
import { useUploadStore } from '@/stores/upload'
import { fetchOriginalBlob } from '@/api'
import { relTime } from '@/utils/format'
import UiEmpty from '@/components/ui/UiEmpty.vue'
import UiButton from '@/components/ui/UiButton.vue'

/**
 * 解析队列 = 由文档状态派生的入库任务（和 agent 的「任务」不是一回事）。
 *
 * 行在解析开始前就写进 documents，所以 OCR 跑着的文档这里马上能看到；
 * stage / progress / parse_plan 由 ingest 子进程分阶段提交，页面按秒轮询。
 */
const jobsStore = useJobsStore()
const ui = useUiStore()
const uploadStore = useUploadStore()
const router = useRouter()
const POLL_MS = 1500
const retrying = ref(null)

let timer = null

onMounted(() => {
  jobsStore.load()
  timer = setInterval(() => jobsStore.load(), POLL_MS)
})

onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
})

/**
 * 失败重试 = 回到上传页，用原文件重开上传对话框。
 *
 * 之前是复制一条 CLI 命令让用户去终端跑；那既要求用户懂命令行，也丢了
 * 第一次上传时选的分区、图片模式和入库备注。现在原文件从服务端取回
 * （还在 data/uploads 下），其余选择由 /api/jobs 一起带回，交给
 * LibraryView 的 UploadDialog 预填。
 */
async function retryUpload(job) {
  if (!job.docId || retrying.value) return
  retrying.value = job.id
  try {
    const blob = await fetchOriginalBlob(job.docId)
    const file = new File([blob], job.name, {
      type: blob.type || 'application/octet-stream',
    })
    uploadStore.setRetry({
      file,
      collectionId: job.collectionId || '',
      imageMode: job.imageMode || 'structured',
      ingestNote: job.ingestNote || '',
    })
    router.push('/library')
  } catch (error) {
    ui.notify(`读取原文件失败：${error.message}`)
  } finally {
    retrying.value = null
  }
}

function percent(job) {
  const value = Number(job.progress)
  if (!Number.isFinite(value)) return job.status === 'failed' ? 100 : 0
  return Math.min(100, Math.max(0, value))
}

/** Long OCR jobs sit at one stage for a minute; elapsed time proves it is alive. */
function elapsed(job) {
  if (!job.startedAt) return ''
  const ms = Date.now() - new Date(job.startedAt).getTime()
  if (!Number.isFinite(ms) || ms < 0) return ''
  const seconds = Math.floor(ms / 1000)
  if (seconds < 60) return `${seconds}s`
  return `${Math.floor(seconds / 60)}m${String(seconds % 60).padStart(2, '0')}s`
}
</script>

<template>
  <section class="page">
    <div class="page-head">
      <div>
        <h1>解析队列</h1>
        <p>正在解析的文档、当前阶段和所用方案；agent 的跨会话工作单元在「任务」页。</p>
      </div>
    </div>

    <div class="panel">
      <div v-if="jobsStore.jobs.length" class="job-list">
        <div v-for="job in jobsStore.jobs" :key="job.id" class="job-row">
          <div class="job-main">
            <div class="job-line">
              <span class="job-name" :title="job.name">{{ job.name }}</span>
              <span class="job-type">{{ job.type }}</span>
              <span v-if="job.plan" class="job-plan">{{ job.plan }}</span>
              <span class="job-state" :class="job.status">
                {{ job.status === 'failed' ? '失败' : '解析中' }}
              </span>
            </div>

            <div class="job-bar" :class="{ failed: job.status === 'failed' }">
              <span :style="{ width: `${percent(job)}%` }"></span>
            </div>

            <div class="job-meta">
              <span class="job-stage">{{ job.stage }}</span>
              <span class="job-time">
                {{ job.status === 'failed' ? relTime(job.time) : `已用时 ${elapsed(job) || '0s'}` }}
              </span>
            </div>
            <div v-if="job.error" class="job-error" :title="job.error">{{ job.error }}</div>
          </div>

          <UiButton
            v-if="job.status === 'failed' && job.docId"
            size="sm"
            :disabled="retrying === job.id"
            title="带着原文件、分区、图片模式和入库备注回到上传页"
            @click="retryUpload(job)"
          >
            {{ retrying === job.id ? '准备中…' : '重新上传' }}
          </UiButton>
        </div>
      </div>
      <UiEmpty
        v-else
        title="解析队列是空的"
        hint="所有文档都已就绪。上传或重新索引时，排队中的文档会出现在这里。"
      />
    </div>
  </section>
</template>

<style scoped>
.job-list { display: flex; flex-direction: column; }
.job-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 11px 16px;
  border-bottom: 1px solid var(--border);
}
.job-row:last-child { border-bottom: 0; }
.job-main { min-width: 0; flex: 1; }
.job-line { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.job-name {
  font-size: 12.5px;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 46ch;
}
.job-type {
  flex: none;
  font-size: 9.5px;
  color: var(--faint);
  padding: 1px 5px;
  border-radius: 3px;
  background: var(--pill);
  text-transform: uppercase;
}
.job-plan {
  flex: none;
  font-size: 10px;
  color: var(--accent);
  background: var(--accent-soft);
  padding: 1px 6px;
  border-radius: 3px;
}
.job-state { flex: none; font-size: 10px; color: var(--muted); }
.job-state.failed { color: var(--danger); }

.job-bar {
  margin-top: 7px;
  height: 3px;
  border-radius: 2px;
  background: var(--pill);
  overflow: hidden;
}
.job-bar span {
  display: block;
  height: 100%;
  border-radius: 2px;
  background: var(--accent);
  transition: width .35s var(--ease);
}
.job-bar.failed span { background: var(--danger); }

.job-meta {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-top: 6px;
  font-size: 10.5px;
  color: var(--faint);
  min-width: 0;
}
.job-stage { flex: none; color: var(--muted); }
.job-time { flex: none; }
.job-error {
  margin-top: 4px;
  font-size: 10.5px;
  line-height: 1.6;
  color: var(--danger);
  word-break: break-word;
}
</style>