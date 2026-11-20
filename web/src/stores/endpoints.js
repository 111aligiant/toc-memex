import { shallowRef } from 'vue'
import { defineStore } from 'pinia'
import {
  createEndpoint,
  deleteEndpoint,
  fetchDefaultEndpoint,
  fetchEndpoints,
  setDefaultEndpoint,
  testEndpoint,
  updateEndpoint,
} from '@/api'

export const useEndpointsStore = defineStore('endpoints', () => {
  const endpoints = shallowRef([])
  const loading = shallowRef(false)

  async function load() {
    loading.value = true
    try {
      const data = await fetchEndpoints()
      endpoints.value = data.endpoints
    } finally {
      loading.value = false
    }
  }

  async function create(data) {
    await createEndpoint(data)
    await load()
  }

  async function update(id, data) {
    await updateEndpoint(id, data)
    await load()
  }

  async function remove(id) {
    await deleteEndpoint(id)
    await load()
  }

  async function test(id) {
    return testEndpoint(id)
  }

  async function makeDefault(id) {
    await setDefaultEndpoint(id)
    await load()
  }

  async function defaultEndpoint() {
    return fetchDefaultEndpoint()
  }

  return { endpoints, loading, load, create, update, remove, test, makeDefault, defaultEndpoint }
})
