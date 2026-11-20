import { ref } from 'vue'
import { defineStore } from 'pinia'

/**
 * Carries a failed ingest back to the upload dialog.
 *
 * 解析队列 retries by reopening the normal upload dialog with the original
 * file and every choice made the first time (collection, image mode,
 * 入库备注). The file is a real File object, so it cannot ride in the URL;
 * this small store is the hand-off between the two pages.
 */
export const useUploadStore = defineStore('upload', () => {
  const retry = ref(null) // { file, collectionId, imageMode, ingestNote }

  function setRetry(payload) {
    retry.value = payload
  }

  function consumeRetry() {
    const payload = retry.value
    retry.value = null
    return payload
  }

  return { retry, setRetry, consumeRetry }
})