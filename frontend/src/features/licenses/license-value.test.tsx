import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { server } from '@/test/msw/server'
import { LicenseValue } from './license-value'
import { licenseStub, noLicenseStub } from './test-fixtures'
import type { DataLicenseSummary } from '@/lib/api/types'

// Counts the detail fetches, so a test can prove the text is not pulled until the dialog opens.
function renderWithText(license: DataLicenseSummary, content: string) {
  const calls = { count: 0 }
  server.use(
    http.get(`http://localhost/api/v1/licenses/${license.id}`, () => {
      calls.count++
      return HttpResponse.json({ ...license, content })
    }),
  )
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <LicenseValue license={license} />
    </QueryClientProvider>,
  )
  return calls
}

describe('LicenseValue', () => {
  it('renders an http(s) reference_url as a link', () => {
    render(
      <LicenseValue
        license={licenseStub('CC-BY-4.0', { reference_url: 'https://example.test/l' })}
      />,
    )
    const link = screen.getByRole('link', { name: 'CC-BY-4.0' })
    expect(link).toHaveAttribute('href', 'https://example.test/l')
  })

  it('opens the text of a tombstoned licence that has no canonical url', async () => {
    // A group keeps resolving a soft-deleted licence (lineage doesn't lapse) and the detail route
    // serves it read-only, so its text stays reachable — this is the row kind with no external
    // source to link out to instead, i.e. the one the in-app text exists for.
    const dead = licenseStub('ACME-1.0', {
      name: 'ACME internal',
      has_content: true,
      reference_url: null,
      deleted_at: '2026-08-01T00:00:00Z',
    })
    renderWithText(dead, 'ACME TERMS')

    await userEvent.click(screen.getByRole('button', { name: 'ACME-1.0' }))

    await waitFor(() => expect(screen.getByText('ACME TERMS')).toBeInTheDocument())
  })

  it('carries the name and short_description in the title tooltip', () => {
    render(
      <LicenseValue
        license={licenseStub('CC-BY-4.0', {
          name: 'Creative Commons Attribution 4.0',
          reference_url: 'https://example.test/l',
          short_description: 'Share and adapt with attribution.',
        })}
      />,
    )
    expect(screen.getByRole('link', { name: 'CC-BY-4.0' })).toHaveAttribute(
      'title',
      'Creative Commons Attribution 4.0 — Share and adapt with attribution.',
    )
  })

  it('uses the name alone as the tooltip when short_description is empty (no dangling em dash)', () => {
    render(
      <LicenseValue
        license={licenseStub('CC-BY-4.0', {
          name: 'Creative Commons Attribution 4.0',
          reference_url: 'https://example.test/l',
          short_description: '',
        })}
      />,
    )
    expect(screen.getByRole('link', { name: 'CC-BY-4.0' })).toHaveAttribute(
      'title',
      'Creative Commons Attribution 4.0',
    )
  })

  it('renders a non-http reference_url as plain text, never a clickable href (stored-XSS guard)', () => {
    render(
      <LicenseValue
        license={licenseStub('CC0-1.0', { reference_url: 'javascript:alert(document.cookie)' })}
      />,
    )
    expect(screen.queryByRole('link')).toBeNull()
    expect(screen.getByText('CC0-1.0')).toBeInTheDocument()
  })

  it('renders a licence carrying in-app text as a dialog trigger, not a link', () => {
    renderWithText(noLicenseStub(), 'NO LICENSE — CLOSED DATA')

    expect(screen.getByRole('button', { name: /no license/i })).toBeInTheDocument()
    expect(screen.queryByRole('link')).toBeNull()
  })

  it('fetches and shows the text only once the dialog is opened', async () => {
    const calls = renderWithText(noLicenseStub(), 'NO LICENSE — CLOSED DATA')
    expect(calls.count).toBe(0)

    await userEvent.click(screen.getByRole('button', { name: /no license/i }))

    await waitFor(() => expect(screen.getByText(/CLOSED DATA/)).toBeInTheDocument())
    expect(calls.count).toBe(1)
  })

  it('exposes the text pane as a focusable, named region', async () => {
    // The pane scrolls, and a scroll container is no tab stop on its own: without this a keyboard
    // reader gets its first screenful and no way to reach the rest. The name carries the licence
    // because a landmark is listed out of context.
    renderWithText(noLicenseStub(), 'NO LICENSE — CLOSED DATA')

    await userEvent.click(screen.getByRole('button', { name: /no license/i }))

    const pane = await screen.findByRole('region', { name: 'License text: No license' })
    expect(pane).toHaveAttribute('tabindex', '0')
  })

  it('keeps the canonical url reachable from inside the dialog', async () => {
    const licensed = licenseStub('ACME-1.0', {
      name: 'ACME internal',
      has_content: true,
      reference_url: 'https://example.test/acme',
    })
    renderWithText(licensed, 'ACME TERMS')

    await userEvent.click(screen.getByRole('button', { name: /acme/i }))

    await waitFor(() => expect(screen.getByText('ACME TERMS')).toBeInTheDocument())
    expect(screen.getByRole('link', { name: /canonical/i })).toHaveAttribute(
      'href',
      'https://example.test/acme',
    )
  })
})
