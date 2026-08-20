import { afterEach, describe, expect, it, vi } from "vitest"

import { uploadPublicChatFiles } from "./public-chat-file-upload"

describe("uploadPublicChatFiles", () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it("sends all files in one authenticated multipart request", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "File is too large" }), {
        status: 413,
        headers: { "Content-Type": "application/json" },
      }),
    )
    const first = new File(["first"], "first.txt", { type: "text/plain" })
    const second = new File(["second"], "second.txt", { type: "text/plain" })

    await expect(uploadPublicChatFiles({
      url: "http://api.local/api/share/files/upload",
      accessToken: "guest-token",
      files: [first, second],
      taskType: "task",
      taskId: 42,
      fallbackError: "Upload failed",
    })).rejects.toThrow("File is too large")

    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [, request] = fetchMock.mock.calls[0]
    expect(new Headers(request?.headers).get("Authorization")).toBe(
      "Bearer guest-token",
    )
    const body = request?.body as FormData
    expect(body.getAll("files")).toEqual([first, second])
    expect(body.get("file")).toBeNull()
    expect(body.get("task_type")).toBe("task")
    expect(body.get("task_id")).toBe("42")
  })

  it("normalizes successful batch metadata", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({
        success: true,
        files: [
          {
            file_id: "file-1",
            filename: "first.txt",
            file_size: 5,
            mime_type: "text/plain",
          },
          {
            file_id: "file-2",
            filename: "second.txt",
            file_size: 6,
            mime_type: "text/plain",
          },
        ],
      }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    )
    const first = new File(["first"], "first.txt", { type: "text/plain" })
    const second = new File(["second"], "second.txt", { type: "text/plain" })

    await expect(uploadPublicChatFiles({
      url: "http://api.local/api/share/files/upload",
      accessToken: "guest-token",
      files: [first, second],
      taskType: "task",
      fallbackError: "Upload failed",
    })).resolves.toEqual([
      { file_id: "file-1", name: "first.txt", size: 5, type: "text/plain" },
      { file_id: "file-2", name: "second.txt", size: 6, type: "text/plain" },
    ])
  })

  it("rejects an incomplete batch response", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({
        success: true,
        files: [{ file_id: "file-1" }],
        message: "Successfully uploaded 2 files",
      }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    )

    await expect(uploadPublicChatFiles({
      url: "http://api.local/api/share/files/upload",
      accessToken: "guest-token",
      files: [new File(["first"], "first.txt"), new File(["second"], "second.txt")],
      taskType: "task",
      fallbackError: "Upload failed",
    })).rejects.toThrow("Upload failed")
  })

  it("does not issue an empty upload request", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")

    await expect(uploadPublicChatFiles({
      url: "http://api.local/api/share/files/upload",
      accessToken: "guest-token",
      files: [],
      taskType: "task",
      fallbackError: "Upload failed",
    })).resolves.toEqual([])
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
