import { describe, expect, it } from "vitest"

import {
  getBuildPreviewTerminalErrorMessage,
  getBuildPreviewWaitingForUser,
} from "./agent-builder-preview-events"

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

describe("getBuildPreviewWaitingForUser", () => {
  it("extracts waiting-for-user messages from normal ReAct task end events", () => {
    expect(
      getBuildPreviewWaitingForUser({
        type: "trace_event",
        event_type: "react_task_end",
        data: {
          result: {
            status: "waiting_for_user",
            message: "How should I use this file?",
            interactions: [
              {
                type: "select_one",
                field: "intent",
                label: "Choose an action",
                options: [{ label: "Summarize", value: "summarize" }],
              },
            ],
          },
        },
      })
    ).toEqual({
      message: "How should I use this file?",
      interactions: [
        {
          type: "select_one",
          field: "intent",
          label: "Choose an action",
          options: [{ label: "Summarize", value: "summarize" }],
        },
      ],
    })
  })

  it("ignores non-waiting task end events", () => {
    expect(
      getBuildPreviewWaitingForUser({
        type: "trace_event",
        event_type: "react_task_end",
        data: {
          result: {
            status: "completed",
            message: "Done",
          },
        },
      })
    ).toBeNull()
  })
})
