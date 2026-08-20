export interface PublicChatUploadedFile {
  file_id: string
  name?: string
  size?: number
  type?: string
}

interface UploadPublicChatFilesOptions {
  url: string
  accessToken: string
  files: File[]
  taskType: string
  taskId?: number | string | null
  fallbackError: string
}

interface PublicChatUploadResponse {
  success?: boolean
  files?: unknown
  detail?: unknown
  message?: unknown
}

function uploadErrorMessage(
  data: PublicChatUploadResponse | null,
  fallbackError: string,
): string {
  return typeof data?.detail === "string"
    ? data.detail
    : typeof data?.message === "string"
      ? data.message
      : fallbackError
}

/** Uploads one public-chat attachment set in a single backend transaction. */
export async function uploadPublicChatFiles({
  url,
  accessToken,
  files,
  taskType,
  taskId,
  fallbackError,
}: UploadPublicChatFilesOptions): Promise<PublicChatUploadedFile[]> {
  if (files.length === 0) return []

  const formData = new FormData()
  files.forEach(file => formData.append("files", file))
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
  const uploaded = Array.isArray(data?.files)
    ? data.files.flatMap((item, index) => {
        if (typeof item !== "object" || item === null) return []
        const record = item as Record<string, unknown>
        if (typeof record.file_id !== "string") return []
        const source = files[index]
        return [{
          file_id: record.file_id,
          name: typeof record.filename === "string" ? record.filename : source?.name,
          size: typeof record.file_size === "number" ? record.file_size : source?.size,
          type: typeof record.mime_type === "string" ? record.mime_type : source?.type,
        }]
      })
    : []

  if (!response.ok || data?.success !== true) {
    throw new Error(uploadErrorMessage(data, fallbackError))
  }
  if (uploaded.length !== files.length) {
    throw new Error(fallbackError)
  }

  return uploaded
}
