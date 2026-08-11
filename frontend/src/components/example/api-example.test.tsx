import React from "react"
import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { ApiExample } from "./api-example"

vi.mock("@/contexts/auth-context", () => ({
  useAuth: () => ({
    user: {
      id: "1",
      username: "acct_0123456789abcdef0123456789abcdef",
      email: "current@example.com",
    },
    token: "access-token",
    refreshToken: "refresh-token",
  }),
}))
vi.mock("@/contexts/i18n-context", () => ({
  useI18n: () => ({ t: (key: string) => key }),
}))
vi.mock("@/hooks/use-api", () => ({
  apiHooks: {
    useGet: () => ({ data: [], loading: false, error: null, refetch: vi.fn() }),
  },
}))
vi.mock("@/hooks/use-websocket", () => ({
  useWebSocket: () => ({ isConnected: false, connectionError: null }),
}))
vi.mock("@/lib/api-wrapper", () => ({ apiRequest: vi.fn() }))
vi.mock("@/lib/utils", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/utils")>()),
  getApiUrl: () => "http://api.test",
}))

describe("ApiExample current-user label", () => {
  beforeEach(() => vi.stubGlobal("React", React))

  afterEach(() => {
    vi.unstubAllGlobals()
    cleanup()
  })

  it("shows email without exposing the opaque username", () => {
    render(<ApiExample />)

    expect(screen.getByText(/current@example\.com/)).toBeInTheDocument()
    expect(screen.queryByText(/acct_0123456789abcdef/)).not.toBeInTheDocument()
  })
})
