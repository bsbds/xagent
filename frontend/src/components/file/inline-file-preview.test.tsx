/// <reference types="@testing-library/jest-dom/vitest" />
import React from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const apiRequestMock = vi.hoisted(() => vi.fn())

vi.mock('@/lib/utils', () => ({
  cn: (...classes: Array<string | undefined | false>) => classes.filter(Boolean).join(' '),
  getApiUrl: () => 'http://api.local',
  getFilePublicPreviewUrl: (fileId: string, apiUrl = 'http://api.local') =>
    `${apiUrl}/api/files/public/preview/${encodeURIComponent(fileId)}`,
}))

vi.mock('@/lib/api-wrapper', () => ({
  apiRequest: apiRequestMock,
}))

vi.mock('@/components/file/docx-preview-renderer', () => ({
  DocxPreviewRenderer: ({ base64Content }: { base64Content: string }) => (
    <div data-testid="docx-preview">{base64Content}</div>
  ),
}))

vi.mock('@/components/file/excel-preview-renderer', () => ({
  ExcelPreviewRenderer: ({ base64Content }: { base64Content: string }) => (
    <div data-testid="excel-preview">{base64Content}</div>
  ),
}))

import { InlineFilePreview } from './inline-file-preview'

describe('InlineFilePreview', () => {
  beforeEach(() => {
    apiRequestMock.mockReset()
  })

  afterEach(() => {
    cleanup()
  })

  it('renders image previews from file ids', () => {
    render(
      <InlineFilePreview
        source={{ type: 'image', fileId: 'image-file-id', filename: 'plot.png' }}
      />
    )

    expect(screen.getByAltText('plot.png')).toHaveAttribute(
      'src',
      'http://api.local/api/files/public/preview/image-file-id'
    )
  })

  it('renders presentation previews in an iframe', () => {
    render(
      <InlineFilePreview
        source={{
          type: 'presentation',
          fileId: 'slides-file-id',
          filename: 'slides.pptx',
        }}
      />
    )

    expect(screen.getByTitle('slides.pptx')).toHaveAttribute(
      'src',
      'http://api.local/api/files/public/preview/slides-file-id'
    )
  })

  it('opens inline previews through the file preview callback when available', () => {
    const handleFileClick = vi.fn()

    render(
      <InlineFilePreview
        source={{
          type: 'presentation',
          fileId: 'slides-file-id',
          filename: 'slides.pptx',
        }}
        onFileClick={handleFileClick}
      />
    )

    fireEvent.click(screen.getByText('Open'))

    expect(handleFileClick).toHaveBeenCalledWith('slides-file-id', 'slides.pptx')
  })

  it('loads document previews through the document renderer', async () => {
    apiRequestMock.mockResolvedValue({
      ok: true,
      arrayBuffer: async () => new Uint8Array([65, 66]).buffer,
    })

    render(
      <InlineFilePreview
        source={{ type: 'document', fileId: 'doc-file-id', filename: 'report.docx' }}
      />
    )

    expect(await screen.findByTestId('docx-preview')).toHaveTextContent('QUI=')
    expect(apiRequestMock).toHaveBeenCalledWith(
      'http://api.local/api/files/public/preview/doc-file-id',
      expect.objectContaining({ cache: 'no-cache' })
    )
  })

  it('loads spreadsheet previews through the spreadsheet renderer', async () => {
    apiRequestMock.mockResolvedValue({
      ok: true,
      arrayBuffer: async () => new Uint8Array([88, 89]).buffer,
    })

    render(
      <InlineFilePreview
        source={{
          type: 'spreadsheet',
          fileId: 'sheet-file-id',
          filename: 'data.xlsx',
        }}
      />
    )

    expect(await screen.findByTestId('excel-preview')).toHaveTextContent('WFk=')
  })
})
