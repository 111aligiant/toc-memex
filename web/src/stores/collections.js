import { computed, shallowRef } from 'vue'
import { defineStore } from 'pinia'
import { fetchCollections } from '@/api'

/**
 * Topic collections, shared by the library, search and document pages.
 *
 * The agent-facing contract lives in the backend: a collection's
 * description is what an agent reads to decide where a request belongs.
 * The UI only needs the list, the counts, and the "unassigned" bucket.
 */
export const useCollectionsStore = defineStore('collections', () => {
  const items = shallowRef([])
  const unassigned = shallowRef(0)
  const loaded = shallowRef(false)
  const loading = shallowRef(false)

  async function load() {
    if (loading.value || loaded.value) return
    loading.value = true
    try {
      const data = await fetchCollections()
      items.value = data.collections
      unassigned.value = data.unassigned
      loaded.value = true
    } finally {
      loading.value = false
    }
  }

  async function refresh() {
    loaded.value = false
    await load()
  }

  /** Options for a filter select: all / unfiled / each collection. */
  const options = computed(() => [
    { value: '', label: '全部分区' },
    { value: 'unassigned', label: '未分区' },
    ...items.value.map((item) => ({ value: item.id, label: item.name })),
  ])

  function byId(id) {
    return items.value.find((item) => item.id === id) || null
  }

  return { items, unassigned, loaded, loading, load, refresh, options, byId }
})
