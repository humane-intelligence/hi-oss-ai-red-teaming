import { describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { toast } from 'sonner'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { TermsSettingsSection } from './terms-settings-section'
import type { MeResponse } from '@/lib/api/types'

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() } }))

const V2 = {
  id: 'terms-2',
  version: '2.0',
  content: 'Second.',
  published_at: '2026-06-01T10:00:00Z',
}
const V1 = { id: 'terms-1', version: '1.0', published_at: '2026-01-01T10:00:00Z' }

function withVersions(items: unknown[] = [V2, V1], currentDocument: unknown = V2) {
  server.use(
    http.get('http://localhost/api/v1/terms', () =>
      HttpResponse.json({ items, total: items.length, limit: 100, offset: 0 }),
    ),
    http.get('http://localhost/api/v1/terms/current', () =>
      currentDocument
        ? HttpResponse.json(currentDocument)
        : HttpResponse.json({ status: 404 }, { status: 404 }),
    ),
  )
}

function renderSection(
  permissions: string[] = ['platform_settings:update'],
  updateUser: (u: MeResponse) => void = () => {},
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(permissions, [], null, { updateUser })
  return render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <TermsSettingsSection />
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

describe('TermsSettingsSection', () => {
  it('names the current version and lists the superseded ones', async () => {
    withVersions()

    renderSection()

    expect(await screen.findByText(/Current version/)).toHaveTextContent('2.0')
    expect(screen.getByText('History')).toBeInTheDocument()
    expect(screen.getByText(/^1\.0 — /)).toBeInTheDocument()
  })

  it('says nothing is published when there are no versions', async () => {
    withVersions([], null)

    renderSection()

    expect(await screen.findByText(/Nothing published yet/)).toBeInTheDocument()
  })

  it('hides the publish form without the update permission', async () => {
    withVersions()

    renderSection(['platform_settings:read'])

    expect(await screen.findByText(/Current version/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Publish' })).not.toBeInTheDocument()
  })

  it('confirms before publishing, then re-gates the publisher', async () => {
    withVersions()
    let posted = false
    const reGated: MeResponse[] = []
    server.use(
      http.post('http://localhost/api/v1/terms', () => {
        posted = true
        return HttpResponse.json({ ...V2, id: 'terms-3', version: '3.0' }, { status: 201 })
      }),
      http.get('http://localhost/api/v1/auth/me', () =>
        HttpResponse.json({
          id: 'u1',
          email: 'a@b.c',
          provider: 'local',
          email_verified: true,
          has_password: true,
          roles: [],
          permissions: ['platform_settings:update'],
          organization: null,
          consent_terms: false,
          consent_emails: false,
          terms_accepted_at: null,
          terms_acceptance_required: true,
        }),
      ),
    )
    const user = userEvent.setup()
    renderSection(['platform_settings:update'], (u) => void reGated.push(u))

    await user.type(await screen.findByLabelText('Version'), '3.0')
    await user.type(screen.getByLabelText('Text (Markdown)'), 'Third.')
    await user.click(screen.getByRole('button', { name: 'Publish' }))

    // The dialog has to name the consequence enforcement gives it: refused by the API, not
    // merely prompted in the console.
    expect(await screen.findByText(/locked out until it accepts this version/)).toBeInTheDocument()
    expect(posted).toBe(false)

    // Both the form's submit and the dialog's confirm are called "Publish"; scope to the dialog.
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Publish' }))
    await waitFor(() => expect(posted).toBe(true))
    // The dialog promises the publisher is gated too, so the held identity has to be re-read.
    await waitFor(() => expect(reGated).toHaveLength(1))
    expect(reGated[0]?.terms_acceptance_required).toBe(true)
  })

  it('does not report a failed publish when only the re-read of the identity failed', async () => {
    // The publish already succeeded and cannot be undone; a throw inside `onSuccess` would fail
    // the whole mutation and put a failure message under a success toast.
    withVersions()
    server.use(
      http.post('http://localhost/api/v1/terms', () =>
        HttpResponse.json({ ...V2, id: 'terms-3', version: '3.0' }, { status: 201 }),
      ),
      http.get('http://localhost/api/v1/auth/me', () => new HttpResponse(null, { status: 500 })),
    )
    const user = userEvent.setup()
    renderSection()

    await user.type(await screen.findByLabelText('Version'), '3.0')
    await user.type(screen.getByLabelText('Text (Markdown)'), 'Third.')
    await user.click(screen.getByRole('button', { name: 'Publish' }))
    await user.click(
      within(await screen.findByRole('dialog')).getByRole('button', { name: 'Publish' }),
    )

    // The form resets, which only happens on the success path.
    await waitFor(() => expect(screen.getByLabelText('Version')).toHaveValue(''))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(toast.warning).toHaveBeenCalledWith(
      'Published. Reload to be asked to accept the new version.',
    )
  })

  it('maps a duplicate version onto the version field, not the form', async () => {
    withVersions()
    server.use(
      http.post('http://localhost/api/v1/terms', () =>
        HttpResponse.json(
          {
            title: 'Conflict',
            status: 409,
            detail: "A terms version '2.0' already exists.",
            errors: [
              {
                loc: ['body', 'version'],
                msg: "A terms version '2.0' already exists.",
                type: 'terms_version_taken',
              },
            ],
          },
          { status: 409 },
        ),
      ),
    )
    const user = userEvent.setup()
    renderSection()

    await user.type(await screen.findByLabelText('Version'), '2.0')
    await user.type(screen.getByLabelText('Text (Markdown)'), 'Clashing.')
    await user.click(screen.getByRole('button', { name: 'Publish' }))
    await user.click(
      within(await screen.findByRole('dialog')).getByRole('button', { name: 'Publish' }),
    )

    // On the field itself, so the operator sees which input to change, and not *also* as the
    // form-level message (`applyApiError` returning true is what suppresses that). `FormField`
    // wires the error into `aria-describedby` and marks the control invalid, so this asserts the
    // accessible description rather than DOM placement; the field error is itself an alert, so
    // "not the form" means the only alert on screen is this field's own.
    const version = await screen.findByLabelText('Version')
    expect(version).toHaveAccessibleDescription(/already exists/)
    expect(version).toBeInvalid()
    const alerts = screen.getAllByRole('alert')
    expect(alerts).toHaveLength(1)
    expect(version.closest('div')).toContainElement(alerts[0]!)
  })

  it('requires a version and a body', async () => {
    withVersions()
    const user = userEvent.setup()
    renderSection()

    await user.click(await screen.findByRole('button', { name: 'Publish' }))

    expect(await screen.findAllByText('Required')).toHaveLength(2)
  })
})
