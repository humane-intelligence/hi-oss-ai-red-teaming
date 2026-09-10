import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { server } from '@/test/msw/server'
import { renderWithProviders } from '@/test/utils'
import { MAX_TAGS, MAX_TAG_KEY_LEN } from '@/lib/api/limits'
import { AllowedTagsCard } from './allowed-tags-card'

const EVAL = 'eval-0001-0000-0000-000000000000'
const url = `http://localhost/api/v1/evaluations/${EVAL}/tag-keys`
const evalUrl = `http://localhost/api/v1/evaluations/${EVAL}`

function keyRow(key: string) {
  return { id: `id-${key}`, evaluation_id: EVAL, key, created_at: '2026-01-01T00:00:00Z' }
}

describe('AllowedTagsCard', () => {
  it('lists keys and says tags are restricted when restriction is on', async () => {
    server.use(http.get(url, () => HttpResponse.json([keyRow('env'), keyRow('team')])))
    renderWithProviders(<AllowedTagsCard evaluationId={EVAL} restricted canManage />)

    await waitFor(() => expect(screen.getByText('env')).toBeInTheDocument())
    expect(screen.getByText('team')).toBeInTheDocument()
    expect(screen.getByText(/restricted to these keys/i)).toBeInTheDocument()
  })

  it('shows the no-restriction hint when the toggle is off', async () => {
    server.use(http.get(url, () => HttpResponse.json([])))
    renderWithProviders(<AllowedTagsCard evaluationId={EVAL} restricted={false} canManage />)

    await waitFor(() => expect(screen.getByText(/no restriction/i)).toBeInTheDocument())
    expect(screen.queryByTestId('allowed-tag-keys')).toBeNull()
  })

  it('warns that all tags are forbidden when restricted with no keys', async () => {
    server.use(http.get(url, () => HttpResponse.json([])))
    renderWithProviders(<AllowedTagsCard evaluationId={EVAL} restricted canManage />)

    await waitFor(() =>
      expect(screen.getByText(/no conversation tags are allowed/i)).toBeInTheDocument(),
    )
  })

  it('toggles restriction by patching the evaluation', async () => {
    let body: unknown = null
    server.use(
      http.get(url, () => HttpResponse.json([])),
      http.patch(evalUrl, async ({ request }) => {
        body = await request.json()
        return HttpResponse.json({}, { status: 200 })
      }),
    )
    const user = userEvent.setup()
    renderWithProviders(<AllowedTagsCard evaluationId={EVAL} restricted={false} canManage />)

    await user.click(await screen.findByRole('checkbox', { name: /restrict tags/i }))
    await waitFor(() => expect(body).toEqual({ tags_restricted: true }))
  })

  it('adds a key and it appears after refetch', async () => {
    const keys: ReturnType<typeof keyRow>[] = []
    server.use(
      http.get(url, () => HttpResponse.json(keys)),
      http.post(url, async ({ request }) => {
        const b = (await request.json()) as { key: string }
        const row = keyRow(b.key)
        keys.push(row)
        return HttpResponse.json(row, { status: 201 })
      }),
    )
    const user = userEvent.setup()
    renderWithProviders(<AllowedTagsCard evaluationId={EVAL} restricted canManage />)

    await user.type(await screen.findByLabelText('New tag key'), 'env')
    await user.click(screen.getByRole('button', { name: /^add$/i }))
    await waitFor(() => expect(screen.getByText('env')).toBeInTheDocument())
  })

  it('removes a key only after the removal is confirmed', async () => {
    // Removing a key while restriction is on forbids it immediately, and every conversation already
    // carrying it stops folding it into the prompt — the same weight as this page's other deletes.
    let deleted: string | null = null
    server.use(
      http.get(url, () => HttpResponse.json([keyRow('env')])),
      http.delete(`${url}/:key`, ({ params }) => {
        deleted = params.key as string
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const user = userEvent.setup()
    renderWithProviders(<AllowedTagsCard evaluationId={EVAL} restricted canManage />)

    await user.click(await screen.findByRole('button', { name: /remove env/i }))
    expect(deleted).toBeNull()

    await user.click(await screen.findByRole('button', { name: /^remove$/i }))
    await waitFor(() => expect(deleted).toBe('env'))
  })

  it('sends nothing when the removal is cancelled', async () => {
    let deleted: string | null = null
    server.use(
      http.get(url, () => HttpResponse.json([keyRow('env')])),
      http.delete(`${url}/:key`, ({ params }) => {
        deleted = params.key as string
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const user = userEvent.setup()
    renderWithProviders(<AllowedTagsCard evaluationId={EVAL} restricted canManage />)

    await user.click(await screen.findByRole('button', { name: /remove env/i }))
    await user.click(await screen.findByRole('button', { name: /^cancel$/i }))

    expect(deleted).toBeNull()
    expect(screen.getByText('env')).toBeInTheDocument()
  })

  it('does not report a key count it has not loaded yet', async () => {
    // "(0)" next to "Loading allowed keys…" reads as "restricted and nothing is allowed", which is
    // the state an admin would act on by adding a key that may already be there.
    server.use(http.get(url, () => HttpResponse.json([keyRow('env')])))
    renderWithProviders(<AllowedTagsCard evaluationId={EVAL} restricted canManage />)

    expect(screen.getByText('Allowed tags')).toBeInTheDocument()
    expect(screen.queryByText(/Allowed tags \(0\)/)).toBeNull()
    expect(await screen.findByText('Allowed tags (1)')).toBeInTheDocument()
  })

  it('is read-only without manage rights (no toggle, add input, or remove buttons)', async () => {
    server.use(http.get(url, () => HttpResponse.json([keyRow('env')])))
    renderWithProviders(<AllowedTagsCard evaluationId={EVAL} restricted canManage={false} />)

    await waitFor(() => expect(screen.getByText('env')).toBeInTheDocument())
    expect(screen.queryByRole('checkbox', { name: /restrict tags/i })).toBeNull()
    expect(screen.queryByLabelText('New tag key')).toBeNull()
    expect(screen.queryByRole('button', { name: /remove env/i })).toBeNull()
  })

  it('surfaces a 422 key error inline', async () => {
    server.use(
      http.get(url, () => HttpResponse.json([])),
      http.post(url, () =>
        HttpResponse.json(
          {
            detail: 'Request body failed validation.',
            errors: [{ loc: ['body', 'key'], msg: 'invalid tag key', type: 'value_error' }],
          },
          { status: 422 },
        ),
      ),
    )
    const user = userEvent.setup()
    renderWithProviders(<AllowedTagsCard evaluationId={EVAL} restricted canManage />)

    await user.type(await screen.findByLabelText('New tag key'), 'bad key')
    await user.click(screen.getByRole('button', { name: /^add$/i }))
    // Announced, like the conversation dialog's inline error: this is the only surface for a 422 whose
    // problem body names the field, so a silent insertion is a failed action a reader never hears.
    expect(await screen.findByRole('alert')).toHaveTextContent('invalid tag key')
  })

  it('caps the key field at the length the API accepts', async () => {
    server.use(http.get(url, () => HttpResponse.json([])))
    renderWithProviders(<AllowedTagsCard evaluationId={EVAL} restricted canManage />)

    expect(await screen.findByLabelText('New tag key')).toHaveAttribute(
      'maxLength',
      String(MAX_TAG_KEY_LEN),
    )
  })

  it('announces the allow-list state and labels Retry while it refetches', async () => {
    // The status slot is a live region so the failure *and* the recovery are spoken, and Retry stays
    // mounted with a label while the refetch runs rather than going inert with no feedback.
    let release: () => void = () => {}
    let calls = 0
    server.use(
      http.get(url, async () => {
        calls += 1
        if (calls === 1) return HttpResponse.json({ detail: 'boom' }, { status: 500 })
        await new Promise<void>((resolve) => {
          release = resolve
        })
        return HttpResponse.json([keyRow('env')])
      }),
    )
    const user = userEvent.setup()
    renderWithProviders(<AllowedTagsCard evaluationId={EVAL} restricted canManage />)

    const status = await waitFor(() => {
      const region = screen.getByRole('status')
      expect(region).toHaveTextContent(/couldn.t load the allowed keys/i)
      return region
    })

    await user.click(screen.getByRole('button', { name: /^retry$/i }))
    // Disabled while the refetch is in flight (and labelled "Retrying…"), so the click has visible
    // feedback instead of an inert control. Matched on the stem so either label satisfies the query.
    await waitFor(() => expect(screen.getByRole('button', { name: /retrying/i })).toBeDisabled())
    expect(status).toHaveTextContent(/retrying the allowed keys/i)

    release()

    // The same live region carries the recovery, so a reader hears it without hunting for the change.
    await waitFor(() => expect(status).toHaveTextContent(/restricted to these keys/i))
  })

  it('clears the inline key error as soon as the key is edited', async () => {
    // Left standing, the message describes input the operator has already replaced.
    server.use(
      http.get(url, () => HttpResponse.json([])),
      http.post(url, () =>
        HttpResponse.json(
          {
            detail: 'Request body failed validation.',
            errors: [{ loc: ['body', 'key'], msg: 'invalid tag key', type: 'value_error' }],
          },
          { status: 422 },
        ),
      ),
    )
    const user = userEvent.setup()
    renderWithProviders(<AllowedTagsCard evaluationId={EVAL} restricted canManage />)

    await user.type(await screen.findByLabelText('New tag key'), 'bad key')
    await user.click(screen.getByRole('button', { name: /^add$/i }))
    expect(await screen.findByText('invalid tag key')).toBeInTheDocument()

    await user.type(screen.getByLabelText('New tag key'), '2')

    expect(screen.queryByText('invalid tag key')).toBeNull()
  })

  it('holds the removal confirm open while the DELETE is in flight', async () => {
    // `ConfirmDialog` disables its Cancel while a write runs, and Esc bypasses a disabled button — so
    // without `busy` the confirm closes itself mid-DELETE, discarding the dialog that named the key.
    let release: () => void = () => {}
    server.use(
      http.get(url, () => HttpResponse.json([keyRow('env')])),
      http.delete(`${url}/:key`, async () => {
        await new Promise<void>((resolve) => {
          release = resolve
        })
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const user = userEvent.setup()
    renderWithProviders(<AllowedTagsCard evaluationId={EVAL} restricted canManage />)
    await user.click(await screen.findByRole('button', { name: /remove env/i }))
    await user.click(await screen.findByRole('button', { name: /^remove$/i }))
    await waitFor(() => expect(screen.getByRole('button', { name: /working/i })).toBeDisabled())

    const esc = new KeyboardEvent('keydown', { key: 'Escape', cancelable: true, bubbles: true })
    screen.getByRole('dialog').dispatchEvent(esc)

    expect(esc.defaultPrevented).toBe(true)
    release()
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('says the allow-list read failed, and retries it, instead of loading forever', async () => {
    // "Loading allowed keys…" on a failed read is the state that gets acted on: the decision this
    // card supports is whether to keep the restriction, and a list that never arrived looks exactly
    // like a list with nothing in it.
    let calls = 0
    server.use(
      http.get(url, () => {
        calls += 1
        return calls === 1
          ? HttpResponse.json({ detail: 'boom' }, { status: 500 })
          : HttpResponse.json([keyRow('env')])
      }),
    )
    const user = userEvent.setup()
    renderWithProviders(<AllowedTagsCard evaluationId={EVAL} restricted canManage />)

    expect(await screen.findByText(/couldn.t load the allowed keys/i)).toBeInTheDocument()
    expect(screen.queryByText(/loading allowed keys/i)).toBeNull()

    await user.click(screen.getByRole('button', { name: /^retry$/i }))

    await waitFor(() => expect(screen.getByText('env')).toBeInTheDocument())
    expect(screen.getByText(/restricted to these keys/i)).toBeInTheDocument()
  })
  it('keeps the loaded keys visible when a refresh fails, and says they may be stale', async () => {
    // Cached data survives a refetch error, so a bare "couldn't load" over the still-rendered list
    // leaves the admin unable to tell whether what they see is current. Adding a key invalidates the
    // query, which is how a refresh happens here without a Retry to click.
    let gets = 0
    server.use(
      http.get(url, () => {
        gets += 1
        return gets === 1
          ? HttpResponse.json([keyRow('env')])
          : HttpResponse.json({ detail: 'boom' }, { status: 500 })
      }),
      http.post(url, () => HttpResponse.json(keyRow('team'), { status: 201 })),
    )
    const user = userEvent.setup()
    renderWithProviders(<AllowedTagsCard evaluationId={EVAL} restricted canManage />)
    await waitFor(() => expect(screen.getByText('env')).toBeInTheDocument())

    await user.type(screen.getByLabelText('New tag key'), 'team')
    await user.click(screen.getByRole('button', { name: /^add$/i }))

    expect(await screen.findByText(/couldn.t refresh the allowed keys/i)).toBeInTheDocument()
    expect(screen.getByText('env')).toBeInTheDocument() // the last known list stays on screen
  })

  it('reports a failed read even when the restriction is off', async () => {
    // The read that failed is the key list, which this card manages whether or not the toggle is on;
    // gating the message on `restricted` rendered a failed list as a loaded-empty one.
    server.use(http.get(url, () => HttpResponse.json({ detail: 'boom' }, { status: 500 })))
    renderWithProviders(<AllowedTagsCard evaluationId={EVAL} restricted={false} canManage />)

    expect(await screen.findByText(/couldn.t load the allowed keys/i)).toBeInTheDocument()
  })

  it('refuses a second Add while the first is still in flight', async () => {
    // The keydown path bypassed the button's `pending` guard, so two quick Enters POSTed twice: the
    // key appeared *and* a 409 toast came back for the same keystroke.
    let posts = 0
    let release: () => void = () => {}
    server.use(
      http.get(url, () => HttpResponse.json([])),
      http.post(url, async () => {
        posts += 1
        await new Promise<void>((resolve) => {
          release = resolve
        })
        return HttpResponse.json(keyRow('env'), { status: 201 })
      }),
    )
    const user = userEvent.setup()
    renderWithProviders(<AllowedTagsCard evaluationId={EVAL} restricted canManage />)
    const input = await screen.findByLabelText('New tag key')
    await user.type(input, 'env')

    await user.keyboard('{Enter}')
    await waitFor(() => expect(posts).toBe(1))
    await user.keyboard('{Enter}')

    expect(posts).toBe(1)
    release()
  })

  it('refuses a key past the cap client-side, and says what the cap is', async () => {
    server.use(
      http.get(url, () =>
        HttpResponse.json(Array.from({ length: MAX_TAGS }, (_, i) => keyRow(`k${i}`))),
      ),
    )
    const user = userEvent.setup()
    renderWithProviders(<AllowedTagsCard evaluationId={EVAL} restricted canManage />)
    await waitFor(() => expect(screen.getByText('k0')).toBeInTheDocument())

    await user.type(screen.getByLabelText('New tag key'), 'one-too-many')

    expect(screen.getByRole('button', { name: /^add$/i })).toBeDisabled()
    expect(screen.getByText(new RegExp(`up to ${MAX_TAGS} allowed keys`, 'i'))).toBeInTheDocument()
  })
})
