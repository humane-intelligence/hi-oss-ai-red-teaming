import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { server } from '@/test/msw/server'
import { renderWithProviders } from '@/test/utils'
import { MessageAttachments } from './message-attachments'

const realCreate = URL.createObjectURL
const realRevoke = URL.revokeObjectURL
beforeAll(() => {
  let n = 0
  URL.createObjectURL = vi.fn(() => `blob:test-${n++}`)
  URL.revokeObjectURL = vi.fn()
})
afterAll(() => {
  URL.createObjectURL = realCreate
  URL.revokeObjectURL = realRevoke
})

function mockImages(behavior: Record<string, 'ok' | 'missing'>) {
  server.use(
    http.get('http://localhost/api/v1/images/signed-url', ({ request }) => {
      const key = new URL(request.url).searchParams.get('key') ?? ''
      return HttpResponse.json({
        url: `/api/v1/images/signed/${encodeURIComponent(key)}`,
        expires_at: '2026-07-17T01:00:00Z',
      })
    }),
    http.get('http://localhost/api/v1/images/signed/:token', ({ params }) => {
      const key = decodeURIComponent(String(params.token))
      if (behavior[key] === 'missing')
        return HttpResponse.json({ title: 'Not Found' }, { status: 404 })
      return new HttpResponse('bytes', { headers: { 'Content-Type': 'image/png' } })
    }),
  )
}

describe('MessageAttachments', () => {
  it('renders a clickable thumbnail per key and opens the lightbox', async () => {
    mockImages({ 'k/a.png': 'ok', 'k/b.png': 'ok' })
    renderWithProviders(<MessageAttachments imageKeys={['k/a.png', 'k/b.png']} />)
    // Each thumbnail resolves on its own fetch, so both need awaiting.
    expect(await screen.findByAltText('Attachment 1')).toBeInTheDocument()
    expect(await screen.findByAltText('Attachment 2')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'View attachment 1 full size' }))
    expect(screen.getByAltText('Attachment 1 full size')).toBeInTheDocument()
    expect(screen.getByText('Attachment 1 of 2')).toBeInTheDocument()
  })

  it('shows an unavailable placeholder for a reaped blob instead of erroring', async () => {
    mockImages({ 'k/gone.png': 'missing' })
    renderWithProviders(<MessageAttachments imageKeys={['k/gone.png']} />)
    await waitFor(() => expect(screen.getByTitle('Attachment unavailable')).toBeInTheDocument())
    expect(screen.queryByAltText('Attachment 1')).not.toBeInTheDocument()
  })

  it('renders nothing for a message without attachments', () => {
    const { container } = renderWithProviders(<MessageAttachments imageKeys={[]} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('steps through attachments with the prev/next buttons', async () => {
    mockImages({ 'k/a.png': 'ok', 'k/b.png': 'ok', 'k/c.png': 'ok' })
    renderWithProviders(<MessageAttachments imageKeys={['k/a.png', 'k/b.png', 'k/c.png']} />)
    await userEvent.click(
      await screen.findByRole('button', { name: 'View attachment 1 full size' }),
    )
    expect(screen.getByText('Attachment 1 of 3')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Next attachment' }))
    expect(screen.getByText('Attachment 2 of 3')).toBeInTheDocument()
    expect(await screen.findByAltText('Attachment 2 full size')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Previous attachment' }))
    expect(screen.getByText('Attachment 1 of 3')).toBeInTheDocument()
  })

  it('steps through attachments with the arrow keys', async () => {
    mockImages({ 'k/a.png': 'ok', 'k/b.png': 'ok' })
    renderWithProviders(<MessageAttachments imageKeys={['k/a.png', 'k/b.png']} />)
    await userEvent.click(
      await screen.findByRole('button', { name: 'View attachment 1 full size' }),
    )
    await userEvent.keyboard('{ArrowRight}')
    expect(screen.getByText('Attachment 2 of 2')).toBeInTheDocument()
    await userEvent.keyboard('{ArrowLeft}')
    expect(screen.getByText('Attachment 1 of 2')).toBeInTheDocument()
  })

  it('disables prev at the first and next at the last attachment', async () => {
    mockImages({ 'k/a.png': 'ok', 'k/b.png': 'ok' })
    renderWithProviders(<MessageAttachments imageKeys={['k/a.png', 'k/b.png']} />)
    await userEvent.click(
      await screen.findByRole('button', { name: 'View attachment 1 full size' }),
    )
    expect(screen.getByRole('button', { name: 'Previous attachment' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Next attachment' })).toBeEnabled()
    await userEvent.click(screen.getByRole('button', { name: 'Next attachment' }))
    expect(screen.getByRole('button', { name: 'Next attachment' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Previous attachment' })).toBeEnabled()
  })

  it('shows no navigation controls for a single attachment', async () => {
    mockImages({ 'k/a.png': 'ok' })
    renderWithProviders(<MessageAttachments imageKeys={['k/a.png']} />)
    await userEvent.click(
      await screen.findByRole('button', { name: 'View attachment 1 full size' }),
    )
    expect(screen.queryByRole('button', { name: 'Next attachment' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Previous attachment' })).not.toBeInTheDocument()
  })
})
