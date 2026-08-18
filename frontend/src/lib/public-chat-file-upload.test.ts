import { afterEach, describe, expect, it, vi } from "vitest"

import {
  uploadDeferredPublicChatFiles,
  uploadPublicChatFile,
} from "./public-chat-file-upload"

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, reject, resolve }
}

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  })
}

describe("uploadPublicChatFile", () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it("rejects backend HTTP failures instead of silently accepting them", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "File is too large" }), {
        status: 413,
        headers: { "Content-Type": "application/json" },
      }),
    )
    const file = new File(["trip"], "trip.txt", { type: "text/plain" })

    await expect(uploadPublicChatFile({
      url: "http://api.local/api/share/files/upload",
      accessToken: "guest-token",
      file,
      taskType: "task",
      taskId: 42,
      fallbackError: "Upload failed",
    })).rejects.toThrow("File is too large")

    const [, request] = fetchMock.mock.calls[0]
    expect(new Headers(request?.headers).get("Authorization")).toBe(
      "Bearer guest-token",
    )
    const body = request?.body as FormData
    expect(body.get("file")).toBe(file)
    expect(body.get("task_type")).toBe("task")
    expect(body.get("task_id")).toBe("42")
  })

  it("returns normalized file metadata for successful uploads", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ success: true, file_id: "file-1" }),
    )
    const file = new File(["trip"], "trip.txt", { type: "text/plain" })

    await expect(uploadPublicChatFile({
      url: "http://api.local/api/share/files/upload",
      accessToken: "guest-token",
      file,
      taskType: "task",
      fallbackError: "Upload failed",
    })).resolves.toEqual({
      file_id: "file-1",
      name: "trip.txt",
      size: 4,
      type: "text/plain",
    })
  })
})

describe("uploadDeferredPublicChatFiles", () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  const options = {
    url: "http://api.local/api/share/files/upload",
    accessToken: "guest-token",
    taskType: "task",
    fallbackError: "Upload failed",
  }

  it("admits uploads FIFO with at most three active requests through drainage", async () => {
    const requests = Array.from({ length: 7 }, () => deferred<Response>())
    const admitted: string[] = []
    let active = 0
    let maxActive = 0
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (_url, init) => {
      const file = (init?.body as FormData).get("file") as File
      const request = requests[admitted.length]
      admitted.push(file.name)
      active += 1
      maxActive = Math.max(maxActive, active)
      try {
        return await request.promise
      } finally {
        active -= 1
      }
    })
    const files = Array.from(
      { length: 7 },
      (_, index) => new File([`${index}`], `file-${index}.txt`),
    )

    const upload = uploadDeferredPublicChatFiles(files, options)

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3))
    expect(admitted).toEqual(["file-0.txt", "file-1.txt", "file-2.txt"])
    for (let index = 0; index < requests.length; index += 1) {
      requests[index].resolve(jsonResponse({
        success: true,
        file_id: `id-${index}`,
      }))
      if (index < requests.length - 3) {
        await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(index + 4))
      }
      expect(active).toBeLessThanOrEqual(3)
    }

    await expect(upload).resolves.toEqual(files.map((file, index) => ({
      file_id: `id-${index}`,
      name: file.name,
      size: file.size,
      type: file.type,
    })))
    expect(admitted).toEqual(files.map((file) => file.name))
    expect(maxActive).toBe(3)
  })

  it("settles every scheduled upload and preserves successful ids after partial failure", async () => {
    const requests = Array.from({ length: 4 }, () => deferred<Response>())
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(
      () => requests[fetchMock.mock.calls.length - 1].promise,
    )
    const files = Array.from(
      { length: 4 },
      (_, index) => new File([`${index}`], `file-${index}.txt`),
    )
    let settled = false

    const upload = uploadDeferredPublicChatFiles(files, options).finally(() => {
      settled = true
    })
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3))
    requests[0].resolve(jsonResponse({ success: true, file_id: "id-0" }))
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4))
    requests[1].resolve(jsonResponse({ detail: "storage unavailable" }, 503))
    requests[2].resolve(jsonResponse({ success: true, file_id: "id-2" }))
    await Promise.resolve()
    expect(settled).toBe(false)
    requests[3].resolve(jsonResponse({ success: true, file_id: "id-3" }))

    await expect(upload).rejects.toThrow("storage unavailable")
    expect((files[0] as File & { file_id?: string }).file_id).toBe("id-0")
    expect((files[1] as File & { file_id?: string }).file_id).toBeUndefined()
    expect((files[2] as File & { file_id?: string }).file_id).toBe("id-2")
    expect((files[3] as File & { file_id?: string }).file_id).toBe("id-3")
  })

  it("skips files that succeeded when the same selection is retried", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResponse({ success: true, file_id: "id-success" }))
      .mockResolvedValueOnce(jsonResponse({ detail: "retry me" }, 503))
      .mockResolvedValueOnce(jsonResponse({ success: true, file_id: "id-retry" }))
    const successful = new File(["ok"], "successful.txt")
    const retry = new File(["retry"], "retry.txt")

    await expect(
      uploadDeferredPublicChatFiles([successful, retry], options),
    ).rejects.toThrow("retry me")
    await expect(
      uploadDeferredPublicChatFiles([successful, retry], options),
    ).resolves.toEqual([
      expect.objectContaining({ file_id: "id-success", name: "successful.txt" }),
      expect.objectContaining({ file_id: "id-retry", name: "retry.txt" }),
    ])

    expect(fetchMock).toHaveBeenCalledTimes(3)
    expect(fetchMock.mock.calls.map(([, init]) =>
      ((init?.body as FormData).get("file") as File).name)).toEqual([
      "successful.txt",
      "retry.txt",
      "retry.txt",
    ])
  })
})
