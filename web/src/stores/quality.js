import { shallowRef } from 'vue'
import { defineStore } from 'pinia'
import {
  approveProposal,
  fetchCuration,
  fetchJobs,
  fetchQuality,
  fetchTasks,
  rejectProposal,
} from '@/api'

export const useQualityStore = defineStore('quality', () => {
  const groups = shallowRef([])
  const checks = shallowRef({})
  const summary = shallowRef(null)
  const loading = shallowRef(false)
  const loaded = shallowRef(false)

  async function load() {
    if (loading.value) return
    loading.value = true
    try {
      const data = await fetchQuality()
      groups.value = data.groups
      checks.value = data.checks
      summary.value = data.summary || null
      loaded.value = true
    } finally {
      loading.value = false
    }
  }

  return { groups, checks, summary, loading, loaded, load }
})

/**
 * Curator proposals awaiting approval (ADR-0002 §13 P3).
 * High-risk ops (page changes, node merges/splits) never apply themselves.
 */
export const useCurationStore = defineStore('curation', () => {
  const proposals = shallowRef([])
  const loading = shallowRef(false)
  const error = shallowRef('')
  const busyId = shallowRef(null)

  async function load() {
    loading.value = true
    error.value = ''
    try {
      const data = await fetchCuration('pending')
      proposals.value = data.proposals || []
    } catch (err) {
      error.value = err.message || '读取失败'
    } finally {
      loading.value = false
    }
  }

  async function approve(id) {
    busyId.value = id
    try {
      await approveProposal(id)
      await load()
    } finally {
      busyId.value = null
    }
  }

  async function reject(id) {
    busyId.value = id
    try {
      await rejectProposal(id)
      await load()
    } finally {
      busyId.value = null
    }
  }

  return { proposals, loading, error, busyId, load, approve, reject }
})

export const useJobsStore = defineStore('jobs', () => {
  const jobs = shallowRef([])
  const loading = shallowRef(false)

  async function load() {
    if (loading.value) return
    loading.value = true
    try {
      const data = await fetchJobs()
      jobs.value = data.jobs
    } finally {
      loading.value = false
    }
  }

  return { jobs, loading, load }
})

/** Activity-layer tasks: cross-agent work units, not the ingest queue. */
export const useTasksStore = defineStore('tasks', () => {
  const tasks = shallowRef([])
  const counts = shallowRef({ open: 0, done: 0, archived: 0 })
  const loading = shallowRef(false)

  async function load() {
    if (loading.value) return
    loading.value = true
    try {
      const data = await fetchTasks()
      tasks.value = data.tasks
      counts.value = data.counts
    } finally {
      loading.value = false
    }
  }

  return { tasks, counts, loading, load }
})
