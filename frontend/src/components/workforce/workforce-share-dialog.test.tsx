/// <reference types="@testing-library/jest-dom/vitest" />
import React from "react"
import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type { WorkforceDetail } from "@/types/workforce"

const fetchDeploymentConfigMock = vi.hoisted(() => vi.fn())
const getWorkforceShareLinkMock = vi.hoisted(() => vi.fn())

vi.mock("@/lib/deployment-config", () => ({
  fetchDeploymentConfig: fetchDeploymentConfigMock,
  buildDeploymentShareUrl: (
    token: string,
    config: { app_origin: string; region: string } | null,
  ) =>
    config
      ? `${config.app_origin}/change-region?region=${config.region}&next=%2Fshare%2F${token}`
      : "",
}))

vi.mock("@/lib/workforces-api", () => ({
  disableWorkforceShareLink: vi.fn(),
  enableWorkforceShareLink: vi.fn(),
  getWorkforceShareLink: getWorkforceShareLinkMock,
  rotateWorkforceShareLink: vi.fn(),
}))

vi.mock("@/lib/browser-location", () => ({
  getBrowserLocationOrigin: () => "https://cloud.example.test",
}))

vi.mock("@/contexts/i18n-context", () => ({
  useI18n: () => ({ t: (key: string) => key }),
}))

import { WorkforceShareDialog } from "./workforce-share-dialog"

const WORKFORCE = {
  id: 42,
  name: "Regional Workforce",
  status: "active",
} as WorkforceDetail

describe("WorkforceShareDialog", () => {
  beforeEach(() => {
    fetchDeploymentConfigMock.mockReset()
    fetchDeploymentConfigMock.mockResolvedValue({
      deployment_origin: "https://sg-origin.cloud.example.test",
      app_origin: "https://cloud.example.test",
      region: "sg",
    })
    getWorkforceShareLinkMock.mockReset()
    getWorkforceShareLinkMock.mockResolvedValue({
      workforce_id: 42,
      share_enabled: true,
      share_token: "regional-share",
      share_updated_at: "2026-07-24T00:00:00Z",
    })
  })

  afterEach(() => {
    cleanup()
  })

  it("builds a canonical share link that bootstraps the owning region", async () => {
    render(
      <WorkforceShareDialog
        workforce={WORKFORCE}
        open
        onClose={vi.fn()}
      />,
    )

    expect(
      await screen.findByDisplayValue(
        "https://cloud.example.test/change-region?region=sg&next=%2Fshare%2Fregional-share",
      ),
    ).toBeInTheDocument()
  })
})
