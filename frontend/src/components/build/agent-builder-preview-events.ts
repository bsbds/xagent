const TERMINAL_PREVIEW_ERROR_TYPES = new Set([
  "agent_error",
  "error",
  "task_error",
  "task_failed",
])

const WAITING_FOR_USER_TRACE_TYPES = new Set([
  "react_task_end",
  "task_end_react",
])

export interface BuildPreviewWaitingForUser {
  message: string
  interactions?: any[]
}

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

export function getBuildPreviewWaitingForUser(message: unknown): BuildPreviewWaitingForUser | null {
  if (!message || typeof message !== "object") {
    return null
  }

  const record = message as Record<string, unknown>
  const type = typeof record.type === "string" ? record.type : ""

  let payload: Record<string, unknown> | null = null
  if (type === "trace_event") {
    const eventType = typeof record.event_type === "string" ? record.event_type : ""
    if (!WAITING_FOR_USER_TRACE_TYPES.has(eventType)) {
      return null
    }

    const data = record.data
    if (!data || typeof data !== "object") {
      return null
    }

    const result = (data as Record<string, unknown>).result
    payload = result && typeof result === "object" ? result as Record<string, unknown> : null
  } else if (type === "task_waiting_for_user") {
    const data = record.data
    payload = data && typeof data === "object" ? data as Record<string, unknown> : record
  }

  if (!payload || (type !== "task_waiting_for_user" && payload.status !== "waiting_for_user")) {
    return null
  }

  const rawMessage = payload.message ?? payload.question
  const waitingMessage = typeof rawMessage === "string" ? rawMessage.trim() : ""
  if (!waitingMessage || waitingMessage === "Task waiting for user response") {
    return null
  }

  const interactions = Array.isArray(payload.interactions) ? payload.interactions : undefined
  return {
    message: waitingMessage,
    ...(interactions && interactions.length > 0 ? { interactions } : {}),
  }
}
