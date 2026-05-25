import React from "react"
import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

const appState = vi.hoisted(() => ({
  messages: [],
  traceEvents: [],
  currentTask: null,
  isProcessing: false,
  isHistoryLoading: false,
  taskId: 42,
  filePreview: { isOpen: false },
  dagExecution: null,
  steps: [],
}))

vi.mock("@/contexts/app-context-chat", () => ({
  useApp: () => ({
    state: appState,
    sendMessage: vi.fn(),
    pauseTask: vi.fn(),
    resumeTask: vi.fn(),
    openFilePreview: vi.fn(),
    closeFilePreview: vi.fn(),
    requestStatus: vi.fn(),
    dispatch: vi.fn(),
  }),
}))

vi.mock("@/contexts/i18n-context", () => ({
  useI18n: () => ({ t: (key: string) => key }),
}))

vi.mock("@/components/chat/ChatMessage", () => ({
  ChatMessage: ({
    content,
    interactionsActive,
    traceEvents,
    taskStatus,
    showEmptyStatus,
  }: {
    content?: string | null
    interactionsActive?: boolean
    traceEvents?: unknown[]
    taskStatus?: string
    showEmptyStatus?: boolean
  }) => (
    <div
      data-testid="chat-message"
      data-active={interactionsActive ? "true" : "false"}
      data-trace-count={traceEvents?.length ?? 0}
      data-task-status={taskStatus || ""}
      data-show-empty-status={showEmptyStatus ? "true" : "false"}
    >
      {content}
    </div>
  ),
}))

vi.mock("@/components/chat/ChatInput", () => ({
  ChatInput: () => <div data-testid="chat-input" />,
}))

vi.mock("@/components/chat/TokenUsageDisplay", () => ({
  TokenUsageDisplay: () => null,
}))

vi.mock("@/components/file/task-file-manager", () => ({
  TaskFileManager: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}))

vi.mock("@/components/file/file-preview-content", () => ({
  FilePreviewContent: () => null,
}))

vi.mock("@/components/file/file-preview-action-buttons", () => ({
  FilePreviewActionButtons: () => null,
}))

vi.mock("@/components/preview-sheet", () => ({
  PreviewSheet: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}))

vi.mock("@/components/layout/center-panel", () => ({
  CenterPanel: () => null,
}))

import { TaskConversationPanel } from "./task-conversation-panel"

describe("TaskConversationPanel", () => {
  afterEach(() => {
    cleanup()
    appState.messages = []
    appState.traceEvents = []
    appState.currentTask = null
    appState.isProcessing = false
    appState.isHistoryLoading = false
  })

  it("renders waiting-for-user prompts from normal task state", () => {
    appState.messages = []
    appState.traceEvents = []
    appState.currentTask = {
      id: "42",
      title: "Preview",
      description: "Preview",
      status: "waiting_for_user",
      createdAt: "2026-01-01T00:00:00Z",
      updatedAt: "2026-01-01T00:00:00Z",
      waitingQuestion: "Which dataset should I use?",
      waitingInteractions: [
        {
          type: "select_one",
          field: "dataset",
          label: "Dataset",
          options: [{ label: "Sales", value: "sales" }],
        },
      ],
    } as any
    appState.isHistoryLoading = false

    render(<TaskConversationPanel mode="embedded-preview" />)

    expect(screen.getByText("Which dataset should I use?")).toBeInTheDocument()
    expect(screen.getByTestId("chat-message")).toHaveAttribute("data-active", "true")
  })

  it("shows history loading before waiting-for-user content while history is loading", () => {
    appState.messages = []
    appState.traceEvents = []
    appState.currentTask = {
      id: "42",
      title: "Task",
      description: "Task",
      status: "waiting_for_user",
      createdAt: "2026-01-01T00:00:00Z",
      updatedAt: "2026-01-01T00:00:00Z",
      waitingQuestion: "Which dataset should I use?",
    } as any
    appState.isHistoryLoading = true

    render(<TaskConversationPanel mode="page" />)

    expect(screen.getByText("common.loading")).toBeInTheDocument()
    expect(screen.queryByText("Which dataset should I use?")).not.toBeInTheDocument()
  })

  it("renders trace process events as separate timeline items between messages", () => {
    appState.messages = [
      {
        id: "msg-user",
        role: "user",
        content: "Run analysis",
        timestamp: "1000",
      },
      {
        id: "msg-result",
        role: "assistant",
        content: "Done",
        timestamp: "3000",
        isResult: true,
      },
    ] as any
    appState.traceEvents = [
      {
        event_id: "trace-1",
        event_type: "tool_call",
        timestamp: 2000,
        data: { message: "Using tool" },
      },
    ] as any
    appState.currentTask = {
      id: "42",
      title: "Task",
      description: "Task",
      status: "completed",
      createdAt: "2026-01-01T00:00:00Z",
      updatedAt: "2026-01-01T00:00:00Z",
    } as any
    appState.isHistoryLoading = false

    render(<TaskConversationPanel mode="page" />)

    const renderedMessages = screen.getAllByTestId("chat-message")
    expect(renderedMessages).toHaveLength(3)
    expect(renderedMessages[0]).toHaveTextContent("Run analysis")
    expect(renderedMessages[1]).toHaveAttribute("data-trace-count", "1")
    expect(renderedMessages[2]).toHaveTextContent("Done")
  })

  it("normalizes invalid timestamps to zero for deterministic ordering", () => {
    appState.messages = [
      {
        id: "msg-valid",
        role: "user",
        content: "Valid timestamp",
        timestamp: "1000",
      },
      {
        id: "msg-invalid",
        role: "assistant",
        content: "Invalid timestamp",
        timestamp: {},
        isResult: true,
      },
    ] as any
    appState.traceEvents = []
    appState.currentTask = {
      id: "42",
      title: "Task",
      description: "Task",
      status: "completed",
      createdAt: "2026-01-01T00:00:00Z",
      updatedAt: "2026-01-01T00:00:00Z",
    } as any

    render(<TaskConversationPanel mode="page" />)

    const renderedMessages = screen.getAllByTestId("chat-message")
    expect(renderedMessages[0]).toHaveTextContent("Invalid timestamp")
    expect(renderedMessages[1]).toHaveTextContent("Valid timestamp")
  })
})
