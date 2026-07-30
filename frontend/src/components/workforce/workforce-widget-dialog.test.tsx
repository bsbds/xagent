/// <reference types="@testing-library/jest-dom/vitest" />
import React from "react"
import { cleanup, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type { WorkforceDetail } from "@/types/workforce"

const fetchDeploymentConfigMock = vi.hoisted(() => vi.fn())
const getWorkforceWidgetConfigMock = vi.hoisted(() => vi.fn())

vi.mock("@/lib/deployment-config", () => ({
  fetchDeploymentConfig: fetchDeploymentConfigMock,
  resolveDeploymentOrigin: (
    config: { deployment_origin: string | null } | null,
    browserOrigin: string,
  ) => config?.deployment_origin || browserOrigin,
}))

vi.mock("@/lib/workforces-api", () => ({
  getWorkforceWidgetConfig: getWorkforceWidgetConfigMock,
  rotateWorkforceWidgetKey: vi.fn(),
  updateWorkforceWidgetConfig: vi.fn(),
}))

vi.mock("@/lib/browser-location", () => ({
  getBrowserLocationOrigin: () => "https://cloud.example.test",
}))

vi.mock("@/contexts/i18n-context", () => ({
  useI18n: () => ({ t: (key: string) => key }),
}))

import { WorkforceWidgetDialog } from "./workforce-widget-dialog"

const WORKFORCE = {
  id: 42,
  name: "Regional Workforce",
  status: "active",
} as WorkforceDetail

describe("WorkforceWidgetDialog", () => {
  beforeEach(() => {
    fetchDeploymentConfigMock.mockReset()
    fetchDeploymentConfigMock.mockResolvedValue({
      deployment_origin: "https://sg-origin.cloud.example.test",
      app_origin: "https://cloud.example.test",
      region: "sg",
    })
    getWorkforceWidgetConfigMock.mockReset()
    getWorkforceWidgetConfigMock.mockResolvedValue({
      workforce_id: 42,
      widget_enabled: true,
      widget_key: "wk-regional",
      allowed_domains: ["*"],
    })
  })

  afterEach(() => {
    cleanup()
  })

  it("builds the embed snippet from the advertised deployment origin", async () => {
    render(
      <WorkforceWidgetDialog
        workforce={WORKFORCE}
        open
        onClose={vi.fn()}
      />,
    )

    await waitFor(() => {
      expect(fetchDeploymentConfigMock).toHaveBeenCalledOnce()
    })
    expect(
      await screen.findByText((content) =>
        content.includes(
          'src="https://sg-origin.cloud.example.test/widget.js"',
        ),
      ),
    ).toBeInTheDocument()
  })
})
