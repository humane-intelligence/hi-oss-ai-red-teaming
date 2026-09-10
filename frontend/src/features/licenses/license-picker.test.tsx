import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useForm, useWatch } from 'react-hook-form'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { server } from '@/test/msw/server'
import { LicensePicker } from './license-picker'
import { licenseStub, noLicenseStub } from './test-fixtures'
import type { DataLicenseSummary } from '@/lib/api/types'

// No default for `inheritLabel` — every case passes one explicitly, and the omitted-prop case
// below needs the real absence (a default would swallow it).
function Harness({
  inheritLabel,
  inheritLicense,
  defaultValue = '',
  offerNoLicense,
  describedBy,
}: {
  inheritLabel?: string
  inheritLicense?: DataLicenseSummary | null
  defaultValue?: string
  offerNoLicense?: boolean
  describedBy?: string
}) {
  const { register, control } = useForm<{ data_license_id: string }>({
    defaultValues: { data_license_id: defaultValue },
  })
  const value = useWatch({ control, name: 'data_license_id' })
  return (
    <LicensePicker
      field={register('data_license_id')}
      value={value}
      inheritLabel={inheritLabel}
      inheritLicense={inheritLicense}
      offerNoLicense={offerNoLicense}
      describedBy={describedBy}
    />
  )
}

function renderFailedCatalog(props: Parameters<typeof Harness>[0] = {}) {
  server.use(
    http.get('http://localhost/api/v1/licenses', () =>
      HttpResponse.json({ detail: 'boom' }, { status: 500 }),
    ),
  )
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <Harness {...props} />
    </QueryClientProvider>,
  )
}

function renderPicker(items: DataLicenseSummary[], props: Parameters<typeof Harness>[0] = {}) {
  server.use(
    http.get('http://localhost/api/v1/licenses', () =>
      HttpResponse.json({ items, total: items.length, limit: 100, offset: 0 }),
    ),
  )
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <Harness {...props} />
    </QueryClientProvider>,
  )
}

describe('LicensePicker', () => {
  it('lists catalog names and labels + describes the inherit option from inheritLicense', async () => {
    const cc0 = licenseStub('CC0-1.0', {
      name: 'CC0 1.0',
      short_description: 'Public-domain dedication.',
    })
    renderPicker([licenseStub('CC-BY-4.0', { name: 'CC BY 4.0', is_default: true }), cc0], {
      inheritLabel: 'Inherit',
      inheritLicense: cc0,
    })

    // Await a catalog-derived option (it renders only once the fetch resolves); the inherit option
    // renders immediately from the passed inheritLicense, so awaiting it wouldn't gate on load.
    expect(await screen.findByRole('option', { name: 'CC BY 4.0' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'CC0 1.0' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: /inherit \(CC0-1\.0\)/i })).toBeInTheDocument()
    // value defaults to '' → inherit selected → the inherit target's gist is described.
    expect(screen.getByText('Public-domain dedication.')).toBeInTheDocument()
  })

  it('describes the currently selected catalog licence', async () => {
    const cc0 = licenseStub('CC0-1.0', {
      name: 'CC0 1.0',
      short_description: 'No rights reserved.',
    })
    renderPicker([licenseStub('CC-BY-4.0', { name: 'CC BY 4.0', is_default: true }), cc0], {
      inheritLabel: 'Inherit',
      defaultValue: cc0.id,
    })

    const desc = await screen.findByText('No rights reserved.')
    // the gist is wired to the select via aria-describedby, not visible-only.
    expect(desc.closest('p')).toHaveAttribute('id', 'data_license_id-hint')
    expect(screen.getByRole('combobox')).toHaveAttribute('aria-describedby', 'data_license_id-hint')
  })

  it('still shows the held value as unlisted when the catalog fetch fails', async () => {
    // The catalog is what supplies the options, so on failure the list is empty and a controlled
    // <select> would display its FIRST option — "Platform default" — while the form still holds (and
    // would re-send) the stored id. Settling the query, not succeeding at it, is what must gate the
    // placeholder.
    const sentinel = noLicenseStub()
    renderFailedCatalog({ inheritLabel: 'Platform default', defaultValue: sentinel.id })

    const select = (await screen.findByLabelText('Data license')) as HTMLSelectElement
    await waitFor(() => expect(select.options.length).toBeGreaterThan(1))
    expect(select.value).toBe(sentinel.id)
    expect(select.options[select.selectedIndex]?.text).toMatch(/not listed/i)
  })

  it('describes the select by both the licence gist and the form note', async () => {
    // Two summands, one attribute: a test with only one of them passes just as well against
    // `describedBy ?? hintId`, which would silence the gist whenever a note is present.
    const cc0 = licenseStub('CC0-1.0', {
      name: 'CC0 1.0',
      short_description: 'No rights reserved.',
    })
    renderPicker([licenseStub('CC-BY-4.0', { is_default: true }), cc0], {
      inheritLabel: 'Platform default',
      defaultValue: cc0.id,
      describedBy: 'outer-note',
    })

    await screen.findByText('No rights reserved.')
    expect(screen.getByRole('combobox')).toHaveAttribute(
      'aria-describedby',
      'data_license_id-hint outer-note',
    )
  })

  it('carries each licence short_description as the option title (hover hint)', async () => {
    renderPicker(
      [licenseStub('CC0-1.0', { name: 'CC0 1.0', short_description: 'No rights reserved.' })],
      {
        inheritLabel: 'Inherit',
      },
    )

    expect(await screen.findByRole('option', { name: 'CC0 1.0' })).toHaveAttribute(
      'title',
      'No rights reserved.',
    )
  })

  it('renders no hint and no aria-describedby when the resolved licence has no short_description', async () => {
    renderPicker([licenseStub('CC-BY-4.0', { name: 'CC BY 4.0', is_default: true })], {
      inheritLabel: 'Platform default',
    })

    // Wait for the catalog so the (absent) hint would have had its chance to render.
    await screen.findByRole('option', { name: /platform default \(CC-BY-4\.0\)/i })
    expect(document.getElementById('data_license_id-hint')).toBeNull()
    expect(screen.getByRole('combobox')).not.toHaveAttribute('aria-describedby')
  })

  it('swaps the hint when the selection actually changes', async () => {
    const user = userEvent.setup()
    const cc0 = licenseStub('CC0-1.0', {
      name: 'CC0 1.0',
      short_description: 'No rights reserved.',
    })
    renderPicker([licenseStub('CC-BY-4.0', { name: 'CC BY 4.0', is_default: true }), cc0], {
      inheritLabel: 'Inherit',
    })

    // Inherit target (CC-BY, no short_description) → no hint until a described licence is picked.
    await screen.findByRole('option', { name: 'CC0 1.0' })
    expect(document.getElementById('data_license_id-hint')).toBeNull()

    await user.selectOptions(screen.getByRole('combobox'), cc0.id)
    expect(await screen.findByText('No rights reserved.')).toBeInTheDocument()
  })

  it('falls back to the platform default when no inheritLicense is given (label + description)', async () => {
    renderPicker(
      [
        licenseStub('CC-BY-4.0', {
          name: 'CC BY 4.0',
          is_default: true,
          short_description: 'Attribution required.',
        }),
      ],
      { inheritLabel: 'Platform default' },
    )

    expect(
      await screen.findByRole('option', { name: /platform default \(CC-BY-4\.0\)/i }),
    ).toBeInTheDocument()
    // value '' → inherit target is the catalog default → its gist is described.
    expect(screen.getByText('Attribution required.')).toBeInTheDocument()
  })

  it('falls back to the default license name when it has no SPDX id (user-authored)', async () => {
    renderPicker(
      [
        licenseStub('X', {
          spdx_id: null,
          name: 'Acme Internal',
          is_curated: false,
          is_default: true,
        }),
      ],
      {
        inheritLabel: 'Platform default',
      },
    )

    expect(
      await screen.findByRole('option', { name: /platform default \(Acme Internal\)/i }),
    ).toBeInTheDocument()
  })

  it('shows a bare inherit label (no parenthetical) when the catalog is empty and no inheritLicense', async () => {
    renderPicker([], { inheritLabel: 'Platform default' })

    const option = await screen.findByRole('option', { name: 'Platform default' })
    expect(option).toBeInTheDocument()
    // Only the inherit sentinel — no catalog options, no "(…)" since there's no default to resolve.
    expect(screen.getAllByRole('option')).toHaveLength(1)
  })

  it('omits the sentinel entirely when no inheritLabel is passed (cascade root)', async () => {
    const cc0 = licenseStub('CC0-1.0', { name: 'CC0 1.0', is_default: false })
    renderPicker([licenseStub('CC-BY-4.0', { name: 'CC BY 4.0', is_default: true }), cc0], {
      defaultValue: cc0.id,
    })

    expect(await screen.findByRole('option', { name: 'CC0 1.0' })).toBeInTheDocument()
    // Only the catalog — no empty option to fall back to, so the selection stays concrete.
    expect(screen.getAllByRole('option').map((o) => o.textContent)).toEqual([
      'CC BY 4.0',
      'CC0 1.0',
    ])
  })

  it('falls to the first licence at the cascade root when the value is empty', async () => {
    // An empty value has no option to name here — the cascade root renders no sentinel. Being
    // controlled does not blank the select: React clears every option's `selected` flag, and the
    // HTML "select one" rule then re-selects the first, exactly as the uncontrolled select did.
    // The form value stays empty, so the required-field check still fires on submit.
    const first = licenseStub('CC-BY-4.0', { name: 'CC BY 4.0', is_default: true })
    renderPicker([first, licenseStub('CC0-1.0', { name: 'CC0 1.0' })])

    const select = (await screen.findByLabelText('Data license')) as HTMLSelectElement
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(2))
    expect(select.value).toBe(first.id)
    expect(select.selectedIndex).toBe(0)
  })

  it('keeps a selected-but-unlisted licence selectable as an orphan option', async () => {
    renderPicker([licenseStub('CC-BY-4.0', { name: 'CC BY 4.0', is_default: true })], {
      inheritLabel: 'Inherit',
      defaultValue: '00000000-0000-0000-0000-0000deadbeef',
    })

    expect(
      await screen.findByRole('option', { name: /referenced license, not listed/i }),
    ).toBeInTheDocument()
  })

  it('shows a preselected license whose option only arrives with the list', async () => {
    // The form prefills from the group before the licenses answer, so at the moment the value is
    // written no matching <option> exists yet. An uncontrolled select drops it and settles on
    // whichever option renders first, while the form state still holds the id — the picker must
    // not disagree. The preselected licence is second in the catalog, so falling back to the first
    // option is distinguishable from honouring the value.
    const ccBy = licenseStub('CC-BY-4.0', { name: 'CC BY 4.0', is_default: true })
    const cc0 = licenseStub('CC0-1.0')
    let release: (() => void) | undefined
    const arrived = new Promise<void>((resolve) => {
      release = resolve
    })
    server.use(
      http.get('http://localhost/api/v1/licenses', async () => {
        await arrived
        return HttpResponse.json({ items: [ccBy, cc0], total: 2, limit: 100, offset: 0 })
      }),
    )
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <Harness defaultValue={cc0.id} />
      </QueryClientProvider>,
    )

    const select = (await screen.findByLabelText('Data license')) as HTMLSelectElement
    release!()

    await waitFor(() => expect(select.value).toBe(cc0.id))
  })

  it('withholds the no-license entry unless the form opts in', async () => {
    renderPicker(
      [licenseStub('CC-BY-4.0', { name: 'CC BY 4.0', is_default: true }), noLicenseStub()],
      { inheritLabel: 'Platform default' },
    )

    // The real catalog row still arrives; only the option is withheld.
    expect(await screen.findByRole('option', { name: 'CC BY 4.0' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'No license' })).not.toBeInTheDocument()
  })

  it('offers the no-license entry when offerNoLicense is set', async () => {
    renderPicker(
      [licenseStub('CC-BY-4.0', { name: 'CC BY 4.0', is_default: true }), noLicenseStub()],
      {
        inheritLabel: 'Platform default',
        offerNoLicense: true,
      },
    )

    expect(await screen.findByRole('option', { name: 'No license' })).toBeInTheDocument()
  })
  it('marks an option whose licence protects conversation data', async () => {
    // This picker is where the decision is taken — since No license became the default for
    // invitation-only groups, choosing here is choosing at-rest encryption. A native <select>
    // renders no markup per option, so the marker has to ride the option text.
    renderPicker(
      [
        licenseStub('CC-BY-4.0', { name: 'CC BY 4.0', is_default: true }),
        licenseStub('CLOSED', { name: 'Closed data', protects_conversation_data: true }),
      ],
      { inheritLabel: 'Platform default' },
    )

    expect(
      await screen.findByRole('option', { name: 'Closed data · protected' }),
    ).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'CC BY 4.0' })).toBeInTheDocument()
  })

  it('marks the inherit sentinel when what it resolves to protects conversation data', async () => {
    // The option most forms sit on by default, and since No license became the default for
    // invitation-only groups it is the one that resolves to a protecting licence.
    renderPicker(
      [
        licenseStub('CLOSED', {
          name: 'Closed data',
          is_default: true,
          protects_conversation_data: true,
        }),
      ],
      { inheritLabel: 'Platform default' },
    )

    expect(
      await screen.findByRole('option', { name: 'Platform default (CLOSED) · protected' }),
    ).toBeInTheDocument()
  })

  it('serves a second mount from cache instead of refetching the catalog', async () => {
    // The picker mounts on three forms; without a window each visit refetched. Safe because
    // changing the platform default invalidates `['licenses']`, which refetches regardless.
    let calls = 0
    server.use(
      http.get('http://localhost/api/v1/licenses', () => {
        calls++
        return HttpResponse.json({
          items: [licenseStub('CC-BY-4.0', { name: 'CC BY 4.0', is_default: true })],
          total: 1,
          limit: 100,
          offset: 0,
        })
      }),
    )
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { unmount } = render(
      <QueryClientProvider client={qc}>
        <Harness inheritLabel="Platform default" />
      </QueryClientProvider>,
    )
    expect(await screen.findByRole('option', { name: 'CC BY 4.0' })).toBeInTheDocument()
    unmount()

    render(
      <QueryClientProvider client={qc}>
        <Harness inheritLabel="Platform default" />
      </QueryClientProvider>,
    )

    expect(await screen.findByRole('option', { name: 'CC BY 4.0' })).toBeInTheDocument()
    expect(calls).toBe(1)
  })
  it('renders the current value even when that entry is not offered', async () => {
    // A controlled <select> whose value matches no option displays the FIRST option, so a group
    // carrying "no licence" would read as whatever sorts first — the opposite of what it is.
    const sentinel = noLicenseStub()
    renderPicker([licenseStub('CC-BY-4.0', { is_default: true }), sentinel], {
      inheritLabel: 'Platform default',
      defaultValue: sentinel.id,
    })

    const select = (await screen.findByLabelText('Data license')) as HTMLSelectElement
    await waitFor(() => expect(select.options.length).toBeGreaterThan(1))
    expect(select.value).toBe(sentinel.id)
    expect(select.options[select.selectedIndex]?.text).toMatch(/no license/i)
  })
})
