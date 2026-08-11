import React from "react"
import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import UserManagement from "./user-management"

const apiRequestMock = vi.hoisted(() => vi.fn())
const authMock = vi.hoisted(() => ({ user: { id: "1", is_admin: true } }))

vi.mock("@/lib/api-wrapper", () => ({ apiRequest: apiRequestMock }))
vi.mock("@/lib/utils", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/utils")>()),
  getApiUrl: () => "http://api.test",
}))
vi.mock("@/contexts/auth-context", () => ({
  useAuth: () => authMock,
}))
vi.mock("@/contexts/i18n-context", () => ({
  useI18n: () => ({ locale: "en", t: (key: string) => key }),
}))
vi.mock("next/link", () => ({
  default: ({ children, ...props }: React.AnchorHTMLAttributes<HTMLAnchorElement>) => (
    <a {...props}>{children}</a>
  ),
}))

describe("UserManagement account labels", () => {
  beforeEach(() => {
    vi.stubGlobal("React", React)
    apiRequestMock.mockReset()
    apiRequestMock.mockResolvedValue({
      ok: true,
      json: async () => ({
        users: [
          {
            id: 2,
            username: "acct_0123456789abcdef0123456789abcdef",
            email: "managed@example.com",
            is_admin: false,
            created_at: "2026-08-11T00:00:00Z",
            updated_at: "2026-08-11T00:00:00Z",
          },
        ],
        total: 1,
        page: 1,
        size: 20,
        pages: 1,
      }),
    })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    cleanup()
  })

  it("shows email instead of an opaque username", async () => {
    render(<UserManagement />)

    expect(await screen.findByText("managed@example.com")).toBeInTheDocument()
    expect(
      screen.queryByText("acct_0123456789abcdef0123456789abcdef"),
    ).not.toBeInTheDocument()
  })
})
