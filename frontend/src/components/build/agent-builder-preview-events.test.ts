import { describe, expect, it } from "vitest"

import { getBuildPreviewTerminalErrorMessage } from "./agent-builder-preview-events"

describe("getBuildPreviewTerminalErrorMessage", () => {
  it("treats normal task-flow agent errors as terminal preview errors", () => {
    expect(
      getBuildPreviewTerminalErrorMessage({
        type: "agent_error",
        message: "Task is currently busy",
      })
    ).toBe("Error: Task is currently busy")
  })

  it("handles generic websocket errors", () => {
    expect(
      getBuildPreviewTerminalErrorMessage({
        type: "error",
        message: "No active agent to pause",
      })
    ).toBe("Error: No active agent to pause")
  })

  it("ignores non-terminal preview events", () => {
    expect(
      getBuildPreviewTerminalErrorMessage({
        type: "task_info",
        status: "running",
      })
    ).toBeNull()
  })
})
