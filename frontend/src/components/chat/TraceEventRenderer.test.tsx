/// <reference types="@testing-library/jest-dom/vitest" />
import React from "react"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}))

vi.mock("@/contexts/i18n-context", () => ({
  useI18n: () => ({
    t: (key: string, vars?: Record<string, string | number>) => {
      if (vars?.tool) return `${key}:${vars.tool}`
      return key
    },
  }),
}))

vi.mock("@/contexts/app-context-chat", () => ({
  useApp: () => ({
    openFilePreview: vi.fn(),
    dispatch: vi.fn(),
  }),
}))

vi.mock("@/lib/utils", async () => {
  const actual = await vi.importActual<typeof import("@/lib/utils")>("@/lib/utils")
  return {
    ...actual,
    getApiUrl: () => "http://api.local",
  }
})

import { TraceEventRenderer } from "./TraceEventRenderer"

describe("TraceEventRenderer", () => {
  afterEach(() => {
    cleanup()
  })

  it("renders image artifacts inline from tool results", async () => {
    render(
      <TraceEventRenderer
        events={[
          {
            event_id: "start",
            event_type: "react_task_start",
            step_id: "step-1",
            timestamp: Date.now(),
            data: { step_name: "Generate image", description: "Generate image" },
          },
          {
            event_id: "tool-start",
            event_type: "tool_execution_start",
            step_id: "step-1",
            timestamp: Date.now(),
            data: { tool_name: "generate_image", tool_args: { prompt: "test" } },
          },
          {
            event_id: "tool-end",
            event_type: "tool_execution_end",
            step_id: "step-1",
            timestamp: Date.now(),
            data: {
              result: {
                success: true,
                artifacts: [
                  {
                    type: "image",
                    file_id: "582e7b79-4de9-4905-b73b-7d5a70ad64fe",
                    filename: "generated_image.png",
                    display: "inline",
                  },
                ],
              },
            },
          },
        ]}
      />,
    )

    fireEvent.click(screen.getByRole("button"))

    const image = screen.getByAltText("generated_image.png")
    expect(image).toHaveAttribute(
      "src",
      "http://api.local/api/files/public/preview/582e7b79-4de9-4905-b73b-7d5a70ad64fe",
    )
  })
})
