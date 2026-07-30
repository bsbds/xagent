import { beforeEach, describe, expect, it, vi } from "vitest"

const apiRequestMock = vi.hoisted(() => vi.fn())
const getApiUrlMock = vi.hoisted(() => vi.fn())

vi.mock("@/lib/api-wrapper", () => ({
  apiRequest: apiRequestMock,
}))

vi.mock("@/lib/utils", () => ({
  getApiUrl: getApiUrlMock,
}))

import {
  buildDeploymentShareUrl,
  fetchDeploymentConfig,
  resolveDeploymentOrigin,
} from "./deployment-config"

describe("deployment config", () => {
  beforeEach(() => {
    apiRequestMock.mockReset()
    getApiUrlMock.mockReset()
    getApiUrlMock.mockReturnValue("")
  })

  it("loads the hosting layer's public deployment targets", async () => {
    apiRequestMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          deployment_origin: "https://sg-origin.cloud.example.test",
          app_origin: "https://cloud.example.test",
          region: "sg",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    )

    await expect(fetchDeploymentConfig()).resolves.toEqual({
      deployment_origin: "https://sg-origin.cloud.example.test",
      app_origin: "https://cloud.example.test",
      region: "sg",
    })
    expect(apiRequestMock).toHaveBeenCalledWith("/api/deployment-config")
  })

  it("uses the explicit deployment origin after configuration loads", () => {
    expect(
      resolveDeploymentOrigin(
        {
          deployment_origin: "https://sg-origin.cloud.example.test/",
          app_origin: "https://cloud.example.test",
          region: "sg",
        },
        "https://cloud.example.test",
      ),
    ).toBe("https://sg-origin.cloud.example.test")
  })

  it("keeps standalone share links direct when no region is advertised", () => {
    expect(
      buildDeploymentShareUrl(
        "share-token",
        {
          deployment_origin: "https://api.example.test",
          app_origin: "https://app.example.test",
          region: null,
        },
        "https://app.example.test",
      ),
    ).toBe("https://app.example.test/share/share-token")
  })

  it("routes regional share links through the canonical region bootstrap", () => {
    expect(
      buildDeploymentShareUrl(
        "share-token",
        {
          deployment_origin: "https://sg-origin.cloud.example.test",
          app_origin: "https://cloud.example.test",
          region: "sg",
        },
        "https://cloud.example.test",
      ),
    ).toBe(
      "https://cloud.example.test/change-region?region=sg&next=%2Fshare%2Fshare-token",
    )
  })
})
