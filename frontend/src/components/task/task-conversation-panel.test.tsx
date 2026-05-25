import React from "react"
import { render, screen } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

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
  ChatMessage: ({ content, interactionsActive }: { content?: string | null; interactionsActive?: boolean }) => (
    <div data-testid="chat-message" data-active={interactionsActive ? "true" : "false"}>
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
})
