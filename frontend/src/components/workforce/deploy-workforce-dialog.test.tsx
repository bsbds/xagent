import React from "react"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

const createWorkforceApiKeyMock = vi.hoisted(() => vi.fn())
const copyToClipboardMock = vi.hoisted(() => vi.fn())
const fetchDeploymentConfigMock = vi.hoisted(() => vi.fn())
const getApiSnippetTargetMock = vi.hoisted(() => vi.fn())
const listAgentApiKeysMock = vi.hoisted(() => vi.fn())
const toastErrorMock = vi.hoisted(() => vi.fn())
const translateMock = vi.hoisted(() => (key: string) => key)

vi.mock("@/lib/agent-api-keys-api", () => ({
  createWorkforceApiKey: createWorkforceApiKeyMock,
  listAgentApiKeys: listAgentApiKeysMock,
}))

vi.mock("@/lib/clipboard", () => ({
  copyToClipboard: copyToClipboardMock,
}))

vi.mock("@/lib/deployment-config", () => ({
  fetchDeploymentConfig: fetchDeploymentConfigMock,
}))

vi.mock("@/lib/api-snippet-base-url", () => ({
  getApiSnippetTarget: getApiSnippetTargetMock,
}))

vi.mock("@/components/ui/sonner", () => ({
  toast: {
    error: toastErrorMock,
  },
}))

vi.mock("@/contexts/i18n-context", () => ({
  useI18n: () => ({ t: translateMock }),
}))

import { DeployWorkforceDialog } from "./deploy-workforce-dialog"

describe("DeployWorkforceDialog", () => {
  beforeEach(() => {
    createWorkforceApiKeyMock.mockReset()
    copyToClipboardMock.mockReset()
    copyToClipboardMock.mockResolvedValue(true)
    fetchDeploymentConfigMock.mockReset()
    fetchDeploymentConfigMock.mockResolvedValue({
      deployment_origin: "https://sg-origin.cloud.example.test",
      app_origin: "https://cloud.example.test",
      region: "sg",
    })
    getApiSnippetTargetMock.mockReset()
    getApiSnippetTargetMock.mockReturnValue({
      baseUrl: "https://sg-origin.cloud.example.test",
    })
    listAgentApiKeysMock.mockReset()
    toastErrorMock.mockReset()
  })

  it("builds API and SDK snippets from the advertised deployment origin", async () => {
    listAgentApiKeysMock.mockResolvedValue([])

    render(
      <DeployWorkforceDialog
        open
        workforceId={42}
        workforceName="Regional Workforce"
        onClose={vi.fn()}
      />,
    )

    await waitFor(() => {
      expect(getApiSnippetTargetMock).toHaveBeenCalledWith(
        "https://sg-origin.cloud.example.test",
      )
    })
    expect(
      screen.getByText((content) =>
        content.includes(
          "https://sg-origin.cloud.example.test/v1/workforces/42/runs",
        ),
      ),
    ).toBeInTheDocument()
  })

  afterEach(() => {
    cleanup()
  })

  it("restores the local API target when deployment config loading fails", async () => {
    fetchDeploymentConfigMock.mockRejectedValue(
      new Error("deployment config unavailable"),
    )
    listAgentApiKeysMock.mockResolvedValue([])

    render(
      <DeployWorkforceDialog
        open
        workforceId={42}
        workforceName="Regional Workforce"
        onClose={vi.fn()}
      />,
    )

    await waitFor(() => {
      expect(getApiSnippetTargetMock).toHaveBeenCalledWith()
    })
    expect(toastErrorMock).toHaveBeenCalledWith(
      "deployment_config.messages.load_failed",
    )
  })

  it("reports snippet clipboard failures", async () => {
    copyToClipboardMock.mockResolvedValue(false)
    listAgentApiKeysMock.mockResolvedValue([])

    render(
      <DeployWorkforceDialog
        open
        workforceId={42}
        workforceName="Regional Workforce"
        onClose={vi.fn()}
      />,
    )

    screen.getByTitle("deploy_workforce.copy").click()

    await vi.waitFor(() => {
      expect(toastErrorMock).toHaveBeenCalledWith(
        "deploy_workforce.copy_failed",
      )
    })
  })

  it("keeps a newly created secret visible when refreshing the key list fails", async () => {
    listAgentApiKeysMock
      .mockResolvedValueOnce([])
      .mockRejectedValueOnce(new Error("refresh failed"))
    createWorkforceApiKeyMock.mockResolvedValue({
      full_key: "xag_test_one_shot_secret",
      key_prefix: "test",
      created_at: "2026-07-23T00:00:00Z",
    })

    render(
      <DeployWorkforceDialog
        open
        workforceId={42}
        workforceName="Review Workforce"
        onClose={vi.fn()}
      />,
    )

    await waitFor(() => {
      expect(listAgentApiKeysMock).toHaveBeenCalledWith({ workforceId: 42 })
    })

    fireEvent.change(
      screen.getByPlaceholderText("deploy_workforce.label_placeholder"),
      { target: { value: "CI" } },
    )
    fireEvent.click(screen.getByRole("button", { name: "deploy_workforce.create_key" }))

    expect(await screen.findByText("xag_test_one_shot_secret")).toBeInTheDocument()
    expect(createWorkforceApiKeyMock).toHaveBeenCalledWith(42, "CI")
    expect(toastErrorMock).toHaveBeenCalledWith("apiKeysPage.messages.loadFailed")
    expect(toastErrorMock).not.toHaveBeenCalledWith(
      "apiKeysPage.messages.createFailed",
    )
  })
})
