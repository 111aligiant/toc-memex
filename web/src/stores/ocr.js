import { shallowRef } from 'vue'
import { defineStore } from 'pinia'
import {
  createOcrEndpoint,
  deleteOcrEndpoint,
  fetchOcrEndpoints,
  setDefaultOcrEndpoint,
  testOcrEndpoint,
  updateOcrEndpoint,
} from '@/api'

export const useOcrStore = defineStore('ocr', () => {
  const endpoints = shallowRef([])
  const active = shallowRef(null)
  const loading = shallowRef(false)

  async function load() {
    loading.value = true
    try {
      const data = await fetchOcrEndpoints()
      endpoints.value = data.endpoints
      active.value = data.active
    } finally {
      loading.value = false
    }
  }

  async function create(data) {
    await createOcrEndpoint(data)
    await load()
  }

  async function update(id, data) {
    await updateOcrEndpoint(id, data)
    await load()
  }

  async function remove(id) {
    await deleteOcrEndpoint(id)
    await load()
  }

  async function test(id) {
    return testOcrEndpoint(id)
  }

  async function makeDefault(id) {
    await setDefaultOcrEndpoint(id)
    await load()
  }

  return { endpoints, active, loading, load, create, update, remove, test, makeDefault }
})
