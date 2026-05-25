const TERMINAL_PREVIEW_ERROR_TYPES = new Set([
  "agent_error",
  "error",
  "task_error",
  "task_failed",
])

export function getBuildPreviewTerminalErrorMessage(message: unknown): string | null {
  if (!message || typeof message !== "object") {
    return null
  }

  const record = message as Record<string, unknown>
  const type = typeof record.type === "string" ? record.type : ""
  if (!TERMINAL_PREVIEW_ERROR_TYPES.has(type)) {
    return null
  }

  const rawMessage = record.error ?? record.message
  const text =
    typeof rawMessage === "string" && rawMessage.trim()
      ? rawMessage.trim()
      : "Preview failed"

  return `Error: ${text}`
}
