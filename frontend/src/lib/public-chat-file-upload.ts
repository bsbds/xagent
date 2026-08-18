export interface PublicChatUploadedFile {
  file_id: string
  name?: string
  size?: number
  type?: string
}

interface UploadPublicChatFileOptions {
  url: string
  accessToken: string
  file: File
  taskType: string
  taskId?: number | string | null
  fallbackError: string
}

type UploadDeferredPublicChatFilesOptions = Omit<UploadPublicChatFileOptions, "file">
type PublicChatFileWithUploadId = File & { file_id?: string }

type ScheduledUpload = {
  run: () => Promise<PublicChatUploadedFile>
  resolve: (file: PublicChatUploadedFile) => void
  reject: (reason: unknown) => void
}

const MAX_ACTIVE_DEFERRED_UPLOADS = 3
const deferredUploadQueue: ScheduledUpload[] = []
let activeDeferredUploads = 0

/**
 * Drain the process-wide browser queue in admission order. A single queue keeps
 * separate public-chat send paths from collectively exceeding the three-upload
 * ceiling when their lifetimes overlap.
 */
function drainDeferredUploadQueue() {
  while (
    activeDeferredUploads < MAX_ACTIVE_DEFERRED_UPLOADS
    && deferredUploadQueue.length > 0
  ) {
    const scheduled = deferredUploadQueue.shift()
    if (!scheduled) {
      return
    }

    activeDeferredUploads += 1
    void scheduled.run()
      .then(scheduled.resolve, scheduled.reject)
      .finally(() => {
        activeDeferredUploads -= 1
        drainDeferredUploadQueue()
      })
  }
}

function scheduleDeferredUpload(
  run: () => Promise<PublicChatUploadedFile>,
): Promise<PublicChatUploadedFile> {
  const scheduled = new Promise<PublicChatUploadedFile>((resolve, reject) => {
    deferredUploadQueue.push({ run, resolve, reject })
  })
  drainDeferredUploadQueue()
  return scheduled
}

interface PublicChatUploadResponse {
  success?: boolean
  file_id?: unknown
  detail?: unknown
  message?: unknown
}

/** Upload one public-chat file without changing the existing request contract. */
export async function uploadPublicChatFile({
  url,
  accessToken,
  file,
  taskType,
  taskId,
  fallbackError,
}: UploadPublicChatFileOptions): Promise<PublicChatUploadedFile> {
  const formData = new FormData()
  formData.append("file", file)
  formData.append("task_type", taskType)
  if (taskId != null) {
    formData.append("task_id", taskId.toString())
  }

  const response = await fetch(url, {
    method: "POST",
    headers: { "Authorization": `Bearer ${accessToken}` },
    body: formData,
  })
  const data = await response.json().catch(() => null) as PublicChatUploadResponse | null
  const fileId = typeof data?.file_id === "string" ? data.file_id : null

  if (!response.ok || data?.success !== true || !fileId) {
    const backendMessage = typeof data?.detail === "string"
      ? data.detail
      : typeof data?.message === "string"
        ? data.message
        : null
    throw new Error(backendMessage || fallbackError)
  }

  return {
    file_id: fileId,
    name: file.name,
    size: file.size,
    type: file.type,
  }
}

/**
 * Upload a deferred selection with bounded FIFO admission.
 *
 * Every selected file is allowed to settle even when another upload fails.
 * Successful ids are written back to their source File, matching the chat
 * transport's pre-uploaded-file convention so a user retry reuses completed
 * work instead of issuing another POST.
 */
export async function uploadDeferredPublicChatFiles(
  files: File[],
  options: UploadDeferredPublicChatFilesOptions,
): Promise<PublicChatUploadedFile[]> {
  const uploads = files.map((file) => {
    const sourceFile = file as PublicChatFileWithUploadId
    if (sourceFile.file_id) {
      return Promise.resolve({
        file_id: sourceFile.file_id,
        name: file.name,
        size: file.size,
        type: file.type,
      })
    }

    return scheduleDeferredUpload(async () => {
      const uploaded = await uploadPublicChatFile({ ...options, file })
      sourceFile.file_id = uploaded.file_id
      return uploaded
    })
  })
  const settled = await Promise.allSettled(uploads)
  const failed = settled.find(
    (result): result is PromiseRejectedResult => result.status === "rejected",
  )

  if (failed) {
    throw failed.reason
  }

  return settled.map((result) => (result as PromiseFulfilledResult<PublicChatUploadedFile>).value)
}
