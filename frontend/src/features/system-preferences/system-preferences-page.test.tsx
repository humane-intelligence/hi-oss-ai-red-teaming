import { describe, expect, it } from 'vitest'
import { delay, http, HttpResponse } from 'msw'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { licenseStub, noLicenseStub } from '@/features/licenses/test-fixtures'
import { SystemPreferencesPage } from './system-preferences-page'
import type { PlatformSettingsResponse } from '@/lib/api/types'

const CC_BY = licenseStub('CC-BY-4.0')
const CC0 = licenseStub('CC0-1.0', { is_default: false })
const NO_LICENSE = noLicenseStub()

const READ_WRITE = ['platform_settings:read', 'platform_settings:update']

function settings(overrides: Partial<PlatformSettingsResponse> = {}): PlatformSettingsResponse {
  return {
    default_license_id: CC_BY.id,
    invite_only: false,
    email_verification_ttl_hours: 24,
    password_min_length: 8,
    password_require_uppercase: false,
    password_require_digit: false,
    password_require_symbol: false,
    password_reset_cooldown_seconds: 60,
    password_reset_max_per_day: 5,
    updated_at: null,
    ...overrides,
  }
}

function mockReads(current: PlatformSettingsResponse) {
  server.use(
    http.get('http://localhost/api/v1/platform-settings', () => HttpResponse.json(current)),
    http.get('http://localhost/api/v1/licenses', () =>
      HttpResponse.json({ items: [CC_BY, CC0, NO_LICENSE], total: 3, limit: 100, offset: 0 }),
    ),
  )
}

function renderPage(perms: string[] = READ_WRITE) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(perms)
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/system-preferences']}>
          <SystemPreferencesPage />
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
  return qc
}

describe('SystemPreferencesPage', () => {
  it('prefills from the current settings', async () => {
    mockReads(settings({ invite_only: true, email_verification_ttl_hours: 72 }))
    renderPage()

    expect(await screen.findByRole('checkbox', { name: /invite-only mode/i })).toBeChecked()
    await waitFor(() => expect(screen.getByLabelText('Data license')).toHaveValue(CC_BY.id))
    expect(screen.getByLabelText('Verification link expiry (hours)')).toHaveValue(72)
    expect(screen.getByText(/shipped defaults/i)).toBeInTheDocument()
  })

  // The Field migration dropped these: `Field` renders a description but wires nothing to it, so
  // every control has to name its own. The checkboxes lost theirs while the numeric inputs kept
  // theirs, which is what made it a regression rather than the general gap.
  it('points every knob at its own visible description', async () => {
    mockReads(settings())
    renderPage()

    await screen.findByRole('checkbox', { name: /invite-only mode/i })

    const described = [
      ['checkbox', /invite-only mode/i],
      ['checkbox', /an uppercase letter/i],
      ['checkbox', /a digit/i],
      ['checkbox', /a symbol/i],
    ] as const

    for (const [role, name] of described) {
      const control = screen.getByRole(role, { name })
      const id = control.getAttribute('aria-describedby')
      expect(id, `${String(name)} has no aria-describedby`).toBeTruthy()
      expect(document.getElementById(id!)).toBeInTheDocument()
    }

    // The three password checkboxes share one hint, hung off each rather than off the fieldset.
    const hint = screen.getByRole('checkbox', { name: /a digit/i }).getAttribute('aria-describedby')
    expect(screen.getByRole('checkbox', { name: /a symbol/i })).toHaveAttribute(
      'aria-describedby',
      hint,
    )
  })

  it('PATCHes only the touched knob, leaving the rest of the singleton alone', async () => {
    mockReads(settings())
    let captured: Record<string, unknown> | null = null
    server.use(
      http.patch('http://localhost/api/v1/platform-settings', async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(
          settings({ invite_only: true, updated_at: '2026-08-11T10:00:00Z' }),
        )
      }),
    )
    const user = userEvent.setup()
    renderPage()

    const save = await screen.findByRole('button', { name: 'Save changes' })
    expect(save).toBeDisabled() // pristine form — nothing to save

    await user.click(screen.getByRole('checkbox', { name: /invite-only mode/i }))
    await user.click(save)

    // A full snapshot here would revert a concurrent admin's license change.
    await waitFor(() => expect(captured).not.toBeNull())
    expect(captured).toEqual({ invite_only: true })
  })

  it('PATCHes the license alone when only the picker moved', async () => {
    mockReads(settings())
    let captured: Record<string, unknown> | null = null
    server.use(
      http.patch('http://localhost/api/v1/platform-settings', async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(settings({ default_license_id: CC0.id }))
      }),
    )
    const user = userEvent.setup()
    renderPage()

    await waitFor(() => expect(screen.getByLabelText('Data license')).toHaveValue(CC_BY.id))
    await user.selectOptions(screen.getByLabelText('Data license'), CC0.id)
    await user.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect(captured).toEqual({ default_license_id: CC0.id })
  })

  it('PATCHes the expiry alone when only it moved', async () => {
    mockReads(settings())
    let captured: Record<string, unknown> | null = null
    server.use(
      http.patch('http://localhost/api/v1/platform-settings', async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(settings({ email_verification_ttl_hours: 48 }))
      }),
    )
    const user = userEvent.setup()
    renderPage()

    const ttl = await screen.findByLabelText('Verification link expiry (hours)')
    await user.clear(ttl)
    await user.type(ttl, '48')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect(captured).toEqual({ email_verification_ttl_hours: 48 })
  })

  it('prefills the password policy and throttling knobs', async () => {
    mockReads(
      settings({
        password_min_length: 16,
        password_require_symbol: true,
        password_reset_cooldown_seconds: 300,
        password_reset_max_per_day: 3,
      }),
    )
    renderPage()

    expect(await screen.findByLabelText('Minimum length (characters)')).toHaveValue(16)
    expect(screen.getByRole('checkbox', { name: /^a symbol/i })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'A digit' })).not.toBeChecked()
    expect(screen.getByLabelText('Reset link cooldown (seconds)')).toHaveValue(300)
    expect(screen.getByLabelText('Reset links per day')).toHaveValue(3)
  })

  it('PATCHes the password knobs alone when only they moved', async () => {
    mockReads(settings())
    let captured: Record<string, unknown> | null = null
    server.use(
      http.patch('http://localhost/api/v1/platform-settings', async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(
          settings({ password_min_length: 12, password_require_digit: true }),
        )
      }),
    )
    const user = userEvent.setup()
    renderPage()

    const minLength = await screen.findByLabelText('Minimum length (characters)')
    await user.clear(minLength)
    await user.type(minLength, '12')
    await user.click(screen.getByRole('checkbox', { name: 'A digit' }))
    await user.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect(captured).toEqual({ password_min_length: 12, password_require_digit: true })
  })

  // A slip between two same-typed branches (`body.password_require_symbol =
  // values.password_require_uppercase`) type-checks, so only a body assertion catches it. Two
  // complementary patterns are needed: three booleans over two values always leave one pair
  // sharing a value, and a swap within that pair is invisible.
  it.each([
    [true, false, false],
    [false, false, true],
  ])(
    'PATCHes every password knob it moved (uppercase=%s digit=%s symbol=%s)',
    async (uppercase, digit, symbol) => {
      mockReads(
        settings({
          password_min_length: 10,
          password_require_uppercase: !uppercase,
          password_require_digit: !digit,
          password_require_symbol: !symbol,
        }),
      )
      let captured: Record<string, unknown> | null = null
      server.use(
        http.patch('http://localhost/api/v1/platform-settings', async ({ request }) => {
          captured = (await request.json()) as Record<string, unknown>
          return HttpResponse.json(settings())
        }),
      )
      const user = userEvent.setup()
      renderPage()

      const minLength = await screen.findByLabelText('Minimum length (characters)')
      await user.clear(minLength)
      await user.type(minLength, '12')
      await user.click(screen.getByRole('checkbox', { name: 'An uppercase letter' }))
      await user.click(screen.getByRole('checkbox', { name: 'A digit' }))
      await user.click(screen.getByRole('checkbox', { name: /^a symbol/i }))
      const cooldown = screen.getByLabelText('Reset link cooldown (seconds)')
      await user.clear(cooldown)
      await user.type(cooldown, '300')
      const perDay = screen.getByLabelText('Reset links per day')
      await user.clear(perDay)
      await user.type(perDay, '3')
      await user.click(screen.getByRole('button', { name: 'Save changes' }))

      await waitFor(() => expect(captured).not.toBeNull())
      expect(captured).toEqual({
        password_min_length: 12,
        password_require_uppercase: uppercase,
        password_require_digit: digit,
        password_require_symbol: symbol,
        password_reset_cooldown_seconds: 300,
        password_reset_max_per_day: 3,
      })
    },
  )

  it('blocks a minimum length below the contract floor before any PATCH', async () => {
    // The API refuses anything under 8, so the form must not spend a round-trip finding out.
    mockReads(settings())
    let patched = false
    server.use(
      http.patch('http://localhost/api/v1/platform-settings', () => {
        patched = true
        return HttpResponse.json(settings())
      }),
    )
    const user = userEvent.setup()
    renderPage()

    const minLength = await screen.findByLabelText('Minimum length (characters)')
    await user.clear(minLength)
    await user.type(minLength, '7')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))

    expect(await screen.findByText('At least 8 characters')).toBeInTheDocument()
    expect(patched).toBe(false)
  })

  it('blocks an out-of-bounds expiry before any PATCH', async () => {
    mockReads(settings())
    let patched = false
    server.use(
      http.patch('http://localhost/api/v1/platform-settings', () => {
        patched = true
        return HttpResponse.json(settings())
      }),
    )
    const user = userEvent.setup()
    renderPage()

    const ttl = await screen.findByLabelText('Verification link expiry (hours)')
    await user.clear(ttl)
    await user.type(ttl, '0')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))

    expect(await screen.findByText('At least 1 hour')).toBeInTheDocument()
    expect(patched).toBe(false)
  })

  it.each([
    ['Reset link cooldown (seconds)', '3601', 'At most 3600 seconds (one hour)'],
    ['Reset links per day', '0', 'At least 1 link'],
  ])('blocks an out-of-bounds %s before any PATCH', async (label, value, message) => {
    // Both bound a security control, so a value the API would refuse must not cost a round-trip
    // — and a widened bound here must not slip past the suite.
    mockReads(settings())
    let patched = false
    server.use(
      http.patch('http://localhost/api/v1/platform-settings', () => {
        patched = true
        return HttpResponse.json(settings())
      }),
    )
    const user = userEvent.setup()
    renderPage()

    const input = await screen.findByLabelText(label)
    await user.clear(input)
    await user.type(input, value)
    await user.click(screen.getByRole('button', { name: 'Save changes' }))

    expect(await screen.findByText(message)).toBeInTheDocument()
    expect(patched).toBe(false)
  })

  it('renders read-only without platform_settings:update', async () => {
    mockReads(settings())
    renderPage(['platform_settings:read'])

    expect(await screen.findByText(/read-only access/i)).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: /invite-only mode/i })).toBeDisabled()
    expect(screen.getByLabelText('Data license')).toBeDisabled()
    expect(screen.getByLabelText('Verification link expiry (hours)')).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Save changes' })).not.toBeInTheDocument()
  })

  it('shows the persisted default even when the catalog loads after the settings', async () => {
    // Regression: an uncontrolled select can't hold a value whose option hasn't rendered yet —
    // resetting the form before the catalog arrived left the picker displaying the first option.
    server.use(
      http.get('http://localhost/api/v1/platform-settings', () => HttpResponse.json(settings())),
      http.get('http://localhost/api/v1/licenses', async () => {
        await delay(50)
        return HttpResponse.json({ items: [CC0, CC_BY], total: 2, limit: 100, offset: 0 })
      }),
    )
    renderPage()

    await waitFor(() => expect(screen.getByLabelText('Data license')).toHaveValue(CC_BY.id))
  })

  it('keeps an in-progress edit when someone else saves the singleton', async () => {
    let current = settings()
    server.use(
      http.get('http://localhost/api/v1/platform-settings', () => HttpResponse.json(current)),
      http.get('http://localhost/api/v1/licenses', () =>
        HttpResponse.json({ items: [CC_BY, CC0], total: 2, limit: 100, offset: 0 }),
      ),
    )
    const user = userEvent.setup()
    const qc = renderPage()

    const checkbox = await screen.findByRole('checkbox', { name: /invite-only mode/i })
    await user.click(checkbox)

    // Another admin changes the license; the refetch must not roll the form back over the edit.
    current = settings({ default_license_id: CC0.id, updated_at: '2026-08-12T10:00:00Z' })
    await act(() => qc.invalidateQueries({ queryKey: ['platform-settings'] }))
    await screen.findByText(/Last changed/) // the new payload has reached the page

    expect(checkbox).toBeChecked()
    expect(screen.getByText('Unsaved changes')).toBeInTheDocument()
  })

  it('offers no inherit sentinel — the platform default is the cascade root', async () => {
    mockReads(settings())
    renderPage()

    const select = await screen.findByLabelText('Data license')
    const labels = Array.from(select.querySelectorAll('option')).map((o) => o.textContent)
    expect(labels).toEqual([CC_BY.name, CC0.name])
  })

  it('withholds the no-license entry — this PATCH would reject it', async () => {
    mockReads(settings())
    renderPage()

    const select = await screen.findByLabelText('Data license')
    const labels = Array.from(select.querySelectorAll('option')).map((o) => o.textContent)
    expect(labels).not.toContain(NO_LICENSE.name)
  })
})
