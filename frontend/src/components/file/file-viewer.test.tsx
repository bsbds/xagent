/// <reference types="@testing-library/jest-dom/vitest" />
import React from "react"
import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

vi.mock("@/lib/utils", async () => {
  const actual = await vi.importActual<typeof import("@/lib/utils")>("@/lib/utils")
  return {
    ...actual,
    getApiUrl: () => "http://api.local",
  }
})

vi.mock("@/contexts/i18n-context", () => ({
  useI18n: () => ({
    t: (key: string) => key,
  }),
}))

import { FileViewer } from "./file-viewer"

describe("FileViewer HTML preview", () => {
  afterEach(() => {
    cleanup()
  })

  it("rewrites file id image sources to public preview URLs", () => {
    render(
      <FileViewer
        fileName="gallery.html"
        fileId="html-file-id"
        content={'<img src="file:582e7b79-4de9-4905-b73b-7d5a70ad64fe">'}
        isLoading={false}
        error={null}
        viewMode="preview"
      />,
    )

    const iframe = screen.getByTitle("gallery.html")

    expect(iframe).toHaveAttribute(
      "srcdoc",
      '<img src="http://api.local/api/files/public/preview/582e7b79-4de9-4905-b73b-7d5a70ad64fe">',
    )
  })

  it("keeps public preview image sources usable in HTML previews", () => {
    render(
      <FileViewer
        fileName="gallery.html"
        fileId="html-file-id"
        content={
          '<img src="/api/files/public/preview/582e7b79-4de9-4905-b73b-7d5a70ad64fe">'
        }
        isLoading={false}
        error={null}
        viewMode="preview"
      />,
    )

    const iframe = screen.getByTitle("gallery.html")

    expect(iframe).toHaveAttribute(
      "srcdoc",
      '<img src="http://api.local/api/files/public/preview/582e7b79-4de9-4905-b73b-7d5a70ad64fe">',
    )
  })

  it("keeps relative HTML assets scoped to the previewed HTML file", () => {
    render(
      <FileViewer
        fileName="gallery.html"
        fileId="html-file-id"
        content={'<img src="./assets/image.png">'}
        isLoading={false}
        error={null}
        viewMode="preview"
      />,
    )

    const iframe = screen.getByTitle("gallery.html")

    expect(iframe).toHaveAttribute(
      "srcdoc",
      '<img src="http://api.local/api/files/public/preview/html-file-id?relative_path=.%2Fassets%2Fimage.png">',
    )
  })
})
