import { shallowRef } from 'vue'
import { defineStore } from 'pinia'
import {
  deleteBenchScenario,
  fetchBenchScenario,
  fetchBenchScenarios,
  submitBench,
} from '@/api'

export const useBenchStore = defineStore('bench', () => {
  const scenarios = shallowRef([])
  const detail = shallowRef(null) // { scenario, runs }
  const loading = shallowRef(false)
  let pollTimer = null

  async function load() {
    loading.value = true
    try {
      const data = await fetchBenchScenarios()
      scenarios.value = data.scenarios
    } finally {
      loading.value = false
    }
    schedulePoll()
  }

  async function open(id) {
    const data = await fetchBenchScenario(id)
    detail.value = data
    schedulePoll()
    return data
  }

  function close() {
    detail.value = null
  }

  async function submit(docIds, endpointIds, ocrEndpointIds = []) {
    const data = await submitBench(docIds, endpointIds, ocrEndpointIds)
    await load()
    return data.scenario
  }

  async function remove(id) {
    await deleteBenchScenario(id)
    if (detail.value?.scenario?.id === id) detail.value = null
    await load()
  }

  function schedulePoll() {
    stopPoll()
    if (scenarios.value.some((s) => s.status === 'processing')) {
      pollTimer = setInterval(async () => {
        await load()
        if (detail.value) {
          try {
            detail.value = await fetchBenchScenario(detail.value.scenario.id)
          } catch {
            detail.value = null // scenario was deleted while polling
          }
        }
      }, 3000)
    }
  }

  function stopPoll() {
    if (pollTimer) {
      clearInterval(pollTimer)
      pollTimer = null
    }
  }

  return { scenarios, detail, loading, load, open, close, submit, remove, stopPoll }
})
