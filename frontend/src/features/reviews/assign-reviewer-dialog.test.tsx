import { afterEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { toast } from 'sonner'
import { server } from '@/test/msw/server'
import { renderWithProviders } from '@/test/utils'
import { AssignReviewerDialog } from './assign-reviewer-dialog'

vi.mock('sonner', () => ({ toast: { success: vi.fn(), warning: vi.fn(), error: vi.fn() } }))
afterEach(() => vi.clearAllMocks())

const FLAG_ID = 'flag-0001-0000-0000-000000000000'
const FLAG_A = 'flag-000a-0000-0000-000000000000'
const FLAG_B = 'flag-000b-0000-0000-000000000000'

type BulkBody = {
  rows: { row_key: string; data: { message_flag_id: string; reviewer_id: string } }[]
}

// Echo every posted row as succeeded (captures the request body for assertions).
function stubBulkEcho(capture: (body: BulkBody) => void) {
  return http.post('http://localhost/api/v1/reviews/bulk', async ({ request }) => {
    const body = (await request.json()) as BulkBody
    capture(body)
    return HttpResponse.json({
      dry_run: false,
      total: body.rows.length,
      succeeded: body.rows.length,
      failed: 0,
      results: body.rows.map((r) => ({
        row_key: r.row_key,
        status: 'ok',
        data: null,
        error: null,
      })),
    })
  })
}

function poolEndpoint(
  flagId: string,
  items: { id: string; email: string; active_review_count?: number }[],
) {
  return http.get(`http://localhost/api/v1/submissions/${flagId}/assignable-reviewers`, () =>
    HttpResponse.json({
      items: items.map((i) => ({ active_review_count: 0, ...i })),
      total: items.length,
      limit: 100,
      offset: 0,
    }),
  )
}

describe('AssignReviewerDialog', () => {
  it('lists candidates from the assignable-reviewers endpoint, not from /users', async () => {
    const usersSpy = vi.fn()
    server.use(
      http.get('http://localhost/api/v1/auth/users', () => {
        usersSpy()
        return HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 })
      }),
      poolEndpoint(FLAG_ID, [{ id: 'u1', email: 'ada@example.com' }]),
    )

    renderWithProviders(
      <AssignReviewerDialog
        flags={[{ id: FLAG_ID, label: 'reason' }]}
        open
        onOpenChange={() => {}}
      />,
    )

    fireEvent.focus(screen.getByLabelText('Reviewers')) // open the combobox list
    // Substring, not exact: the option is announced as the address plus the workload hint, which is
    // decision-relevant to a screen-reader user and so deliberately part of the accessible name.
    expect(await screen.findByRole('option', { name: /ada@example\.com/ })).toBeInTheDocument()
    expect(usersSpy).not.toHaveBeenCalled()
  })

  it('shows an empty state when no eligible reviewers remain', async () => {
    server.use(poolEndpoint(FLAG_ID, []))

    renderWithProviders(
      <AssignReviewerDialog
        flags={[{ id: FLAG_ID, label: 'reason' }]}
        open
        onOpenChange={() => {}}
      />,
    )

    fireEvent.focus(screen.getByLabelText('Reviewers')) // open the combobox list
    // The empty label also renders in the combobox's sr-only live region; scope to the visible
    // listbox row so the match stays unambiguous.
    const listbox = await screen.findByRole('listbox')
    expect(await within(listbox).findByText(/no eligible reviewers/i)).toBeInTheDocument()
  })

  it('single-flag: assigns several reviewers to the one flag (regression)', async () => {
    let body: BulkBody | null = null
    server.use(
      poolEndpoint(FLAG_ID, [
        { id: 'u1', email: 'ada@example.com' },
        { id: 'u2', email: 'bo@example.com' },
      ]),
      stubBulkEcho((b) => (body = b)),
    )
    const u = userEvent.setup()
    renderWithProviders(
      <AssignReviewerDialog
        flags={[{ id: FLAG_ID, label: 'reason' }]}
        open
        onOpenChange={() => {}}
      />,
    )

    await u.click(await screen.findByLabelText('Reviewers'))
    await u.click(await screen.findByRole('option', { name: /ada@example\.com/ }))
    await u.click(await screen.findByRole('option', { name: /bo@example\.com/ }))
    await u.click(screen.getByRole('button', { name: /^Assign/ }))

    await waitFor(() => expect(body).not.toBeNull())
    expect(body!.rows.map((r) => r.row_key).sort()).toEqual([`${FLAG_ID}:u1`, `${FLAG_ID}:u2`])
    expect(body!.rows.every((r) => r.data.message_flag_id === FLAG_ID)).toBe(true)
  })

  it('assigns the cartesian of picked reviewers × picked flags', async () => {
    let body: BulkBody | null = null
    server.use(
      poolEndpoint(FLAG_A, [
        { id: 'u1', email: 'ada@example.com' },
        { id: 'u2', email: 'bo@example.com' },
      ]),
      poolEndpoint(FLAG_B, [
        { id: 'u1', email: 'ada@example.com' },
        { id: 'u2', email: 'bo@example.com' },
      ]),
      stubBulkEcho((b) => (body = b)),
    )
    const u = userEvent.setup()
    renderWithProviders(
      <AssignReviewerDialog
        flags={[
          { id: FLAG_A, label: 'reason A' },
          { id: FLAG_B, label: 'reason B' },
        ]}
        open
        onOpenChange={() => {}}
      />,
    )

    await u.click(await screen.findByLabelText('Reviewers'))
    await u.click(await screen.findByRole('option', { name: /ada@example\.com/ }))
    await u.click(await screen.findByRole('option', { name: /bo@example\.com/ }))
    await u.click(screen.getByRole('button', { name: /^Assign/ }))

    await waitFor(() => expect(body).not.toBeNull())
    expect(body!.rows.map((r) => r.row_key).sort()).toEqual([
      `${FLAG_A}:u1`,
      `${FLAG_A}:u2`,
      `${FLAG_B}:u1`,
      `${FLAG_B}:u2`,
    ])
  })

  it('pre-filters pairs a flag pool does not contain', async () => {
    let body: BulkBody | null = null
    server.use(
      poolEndpoint(FLAG_A, [{ id: 'u1', email: 'ada@example.com' }]), // A: only u1
      poolEndpoint(FLAG_B, [
        { id: 'u1', email: 'ada@example.com' },
        { id: 'u2', email: 'bo@example.com' },
      ]),
      stubBulkEcho((b) => (body = b)),
    )
    const u = userEvent.setup()
    renderWithProviders(
      <AssignReviewerDialog
        flags={[
          { id: FLAG_A, label: 'reason A' },
          { id: FLAG_B, label: 'reason B' },
        ]}
        open
        onOpenChange={() => {}}
      />,
    )

    await u.click(await screen.findByLabelText('Reviewers'))
    await u.click(await screen.findByRole('option', { name: /ada@example\.com/ })) // u1
    await u.click(await screen.findByRole('option', { name: /bo@example\.com/ })) // u2 (only eligible for B)
    await u.click(screen.getByRole('button', { name: /^Assign/ }))

    await waitFor(() => expect(body).not.toBeNull())
    // A:u2 dropped — u2 is not in A's pool; genuine race pairs would still surface per-row.
    expect(body!.rows.map((r) => r.row_key).sort()).toEqual([
      `${FLAG_A}:u1`,
      `${FLAG_B}:u1`,
      `${FLAG_B}:u2`,
    ])
  })

  it('keeps a reviewer picked under an earlier search when the search narrows away (P1 regression)', async () => {
    let body: BulkBody | null = null
    // Pool narrows by the ?search= param, like the backend — so a picked reviewer can scroll out of the result set.
    server.use(
      http.get(
        `http://localhost/api/v1/submissions/${FLAG_ID}/assignable-reviewers`,
        ({ request }) => {
          const q = new URL(request.url).searchParams.get('search')?.toLowerCase() ?? ''
          const all = [
            { id: 'u1', email: 'ada@example.com' },
            { id: 'u2', email: 'bob@example.com' },
          ]
          const items = q ? all.filter((r) => r.email.toLowerCase().includes(q)) : all
          return HttpResponse.json({ items, total: items.length, limit: 100, offset: 0 })
        },
      ),
      stubBulkEcho((b) => (body = b)),
    )
    const u = userEvent.setup()
    renderWithProviders(
      <AssignReviewerDialog
        flags={[{ id: FLAG_ID, label: 'reason' }]}
        open
        onOpenChange={() => {}}
      />,
    )

    const input = await screen.findByLabelText('Reviewers')
    await u.click(input)
    await u.click(await screen.findByRole('option', { name: /ada@example\.com/ })) // pick ada from the full pool
    await u.type(input, 'bob') // narrow the server search away from ada (debounced)
    await waitFor(() =>
      expect(screen.queryByRole('option', { name: /ada@example\.com/ })).not.toBeInTheDocument(),
    )
    await u.click(await screen.findByRole('option', { name: /bob@example\.com/ })) // pick bob from the narrowed pool
    await u.click(screen.getByRole('button', { name: /^Assign/ }))

    await waitFor(() => expect(body).not.toBeNull())
    // ada was picked before the narrowing — she must still be POSTed, not silently dropped.
    expect(body!.rows.map((r) => r.row_key).sort()).toEqual([`${FLAG_ID}:u1`, `${FLAG_ID}:u2`])
  })

  it('surfaces skipped pairs where a picked reviewer is not eligible for a selected flag', async () => {
    server.use(
      poolEndpoint(FLAG_A, [{ id: 'u1', email: 'ada@example.com' }]), // A: only u1
      poolEndpoint(FLAG_B, [
        { id: 'u1', email: 'ada@example.com' },
        { id: 'u2', email: 'bob@example.com' },
      ]),
    )
    const u = userEvent.setup()
    renderWithProviders(
      <AssignReviewerDialog
        flags={[
          { id: FLAG_A, label: 'reason A' },
          { id: FLAG_B, label: 'reason B' },
        ]}
        open
        onOpenChange={() => {}}
      />,
    )

    await u.click(await screen.findByLabelText('Reviewers'))
    await u.click(await screen.findByRole('option', { name: /ada@example\.com/ })) // u1: eligible for A and B
    await u.click(await screen.findByRole('option', { name: /bob@example\.com/ })) // u2: eligible for B only

    // bob→A is skipped (not in A's pool) — shown explicitly, not just implied by a lower total.
    const block = (await screen.findByText(/not in the candidate list/i)).closest('div')
    expect(block?.textContent).toMatch(/bob@example\.com/)
    expect(block?.textContent).toMatch(/reason A/)
    // ada is eligible everywhere, so she is never listed as skipped.
    expect(block?.textContent).not.toMatch(/ada@example\.com/)
  })

  // The visible picker list reflects the CURRENT search — it must not accumulate every reviewer seen
  // this session (options current-scoped; only poolByFlag accumulates, for the pre-filter).
  it('narrows the picker list as the search changes', async () => {
    server.use(
      http.get(
        `http://localhost/api/v1/submissions/${FLAG_ID}/assignable-reviewers`,
        ({ request }) => {
          const q = new URL(request.url).searchParams.get('search')?.toLowerCase() ?? ''
          const all = [
            { id: 'u1', email: 'ada@example.com' },
            { id: 'u2', email: 'bob@example.com' },
          ]
          const items = q ? all.filter((r) => r.email.toLowerCase().includes(q)) : all
          return HttpResponse.json({ items, total: items.length, limit: 100, offset: 0 })
        },
      ),
    )
    const u = userEvent.setup()
    renderWithProviders(
      <AssignReviewerDialog
        flags={[{ id: FLAG_ID, label: 'reason' }]}
        open
        onOpenChange={() => {}}
      />,
    )

    const input = await screen.findByLabelText('Reviewers')
    await u.click(input)
    await screen.findByRole('option', { name: /ada@example\.com/ }) // broad search lists both
    await u.type(input, 'bob') // narrow to bob
    await screen.findByRole('option', { name: /bob@example\.com/ })
    // ada drops out of the list once the search excludes her (options are not accumulated).
    await waitFor(() =>
      expect(screen.queryByRole('option', { name: /ada@example\.com/ })).not.toBeInTheDocument(),
    )
  })

  it('failure summary names the flag and the reviewer', async () => {
    server.use(
      poolEndpoint(FLAG_A, [{ id: 'u1', email: 'ada@example.com' }]),
      http.post('http://localhost/api/v1/reviews/bulk', async ({ request }) => {
        const b = (await request.json()) as BulkBody
        return HttpResponse.json({
          dry_run: false,
          total: b.rows.length,
          succeeded: 0,
          failed: b.rows.length,
          results: b.rows.map((r) => ({
            row_key: r.row_key,
            status: 'failed',
            data: null,
            error: {
              title: 'Conflict',
              detail: 'Reviewer is already assigned to this submission.',
              status: 409,
            },
          })),
        })
      }),
    )
    const u = userEvent.setup()
    renderWithProviders(
      <AssignReviewerDialog
        flags={[{ id: FLAG_A, label: 'reason A' }]}
        open
        onOpenChange={() => {}}
      />,
    )

    await u.click(await screen.findByLabelText('Reviewers'))
    await u.click(await screen.findByRole('option', { name: /ada@example\.com/ }))
    await u.click(screen.getByRole('button', { name: /^Assign/ }))

    const summary = await screen.findByText(/already assigned/i)
    expect(summary.textContent).toMatch(/ada@example\.com/)
    expect(summary.textContent).toMatch(/reason A/)
  })

  // One reviewer succeeds, one fails: the summary names the failure by email, the dialog stays open,
  // and the partial-failure toast fires (not the success one).
  it('surfaces a partial failure by email, keeps the dialog open, and warns', async () => {
    server.use(
      http.get(`http://localhost/api/v1/submissions/${FLAG_ID}/assignable-reviewers`, () =>
        HttpResponse.json({
          items: [
            { id: 'u1', email: 'ada@example.com' },
            { id: 'u2', email: 'bo@example.com' },
          ],
          total: 2,
          limit: 100,
          offset: 0,
        }),
      ),
      http.post('http://localhost/api/v1/reviews/bulk', async ({ request }) => {
        const b = (await request.json()) as {
          rows: { row_key: string; data: { reviewer_id: string } }[]
        }
        return HttpResponse.json({
          dry_run: false,
          total: b.rows.length,
          succeeded: b.rows.length - 1,
          failed: 1,
          results: b.rows.map((r, i) => ({
            row_key: r.row_key,
            status: i === b.rows.length - 1 ? 'failed' : 'ok',
            data: null,
            error:
              i === b.rows.length - 1
                ? {
                    title: 'Conflict',
                    detail: 'Reviewer is already assigned to this submission.',
                    status: 409,
                  }
                : null,
          })),
        })
      }),
    )
    const onOpenChange = vi.fn()
    const u = userEvent.setup()
    renderWithProviders(
      <AssignReviewerDialog
        flags={[{ id: FLAG_ID, label: 'reason' }]}
        open
        onOpenChange={onOpenChange}
      />,
    )

    await u.click(await screen.findByLabelText('Reviewers'))
    await u.click(await screen.findByRole('option', { name: /ada@example\.com/ }))
    await u.click(await screen.findByRole('option', { name: /bo@example\.com/ }))
    await u.click(screen.getByRole('button', { name: /^Assign/ }))

    const summary = await screen.findByText(/1 failed/i)
    // Failure named by email, not a raw UUID.
    expect(summary.parentElement?.textContent).toMatch(/bo@example\.com/)
    expect(summary.parentElement?.textContent).toMatch(/already assigned/i)
    expect(onOpenChange).not.toHaveBeenCalledWith(false) // dialog stays open on partial failure
    expect(toast.warning).toHaveBeenCalled()
    expect(toast.success).not.toHaveBeenCalled()
  })

  // After a partial failure, re-clicking Assign must NOT re-post the reviewers that already succeeded
  // (they would come back as 409). The succeeded rows are pruned; only the failure is retried.
  it('prunes succeeded reviewers so a retry re-posts only the failures', async () => {
    const bodies: { rows: { row_key: string; data: { reviewer_id: string } }[] }[] = []
    server.use(
      http.get(`http://localhost/api/v1/submissions/${FLAG_ID}/assignable-reviewers`, () =>
        HttpResponse.json({
          items: [
            { id: 'u1', email: 'ada@example.com' },
            { id: 'u2', email: 'bo@example.com' },
          ],
          total: 2,
          limit: 100,
          offset: 0,
        }),
      ),
      http.post('http://localhost/api/v1/reviews/bulk', async ({ request }) => {
        const b = (await request.json()) as {
          rows: { row_key: string; data: { reviewer_id: string } }[]
        }
        bodies.push(b)
        // u1 (row 0) ok, u2 (row 1) fails on the first batch; on a retry every remaining row fails too.
        return HttpResponse.json({
          dry_run: false,
          total: b.rows.length,
          succeeded: b.rows.filter((r) => r.data.reviewer_id === 'u1').length,
          failed: b.rows.filter((r) => r.data.reviewer_id !== 'u1').length,
          results: b.rows.map((r) => ({
            row_key: r.row_key,
            status: r.data.reviewer_id === 'u1' ? 'ok' : 'failed',
            data: null,
            error:
              r.data.reviewer_id === 'u1'
                ? null
                : { title: 'Conflict', detail: 'race', status: 409 },
          })),
        })
      }),
    )
    const u = userEvent.setup()
    renderWithProviders(
      <AssignReviewerDialog
        flags={[{ id: FLAG_ID, label: 'reason' }]}
        open
        onOpenChange={() => {}}
      />,
    )

    await u.click(await screen.findByLabelText('Reviewers'))
    await u.click(await screen.findByRole('option', { name: /ada@example\.com/ }))
    await u.click(await screen.findByRole('option', { name: /bo@example\.com/ }))
    await u.click(screen.getByRole('button', { name: /^Assign/ }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    await u.click(screen.getByRole('button', { name: /^Assign/ })) // retry
    await waitFor(() => expect(bodies).toHaveLength(2))

    // First batch posted both; the retry posts ONLY the previously-failed reviewer.
    expect(bodies[0]!.rows.map((r) => r.data.reviewer_id).sort()).toEqual(['u1', 'u2'])
    expect(bodies[1]!.rows.map((r) => r.data.reviewer_id)).toEqual(['u2'])
  })

  // A reviewer picked before a search filter, who then fails, must render by email — the summary
  // accumulates id→email rather than reading only the current search-filtered candidate page.
  it('names a failed reviewer by email even after the search narrowed them out', async () => {
    server.use(
      http.get(
        `http://localhost/api/v1/submissions/${FLAG_ID}/assignable-reviewers`,
        ({ request }) => {
          const q = new URL(request.url).searchParams.get('search')?.toLowerCase() ?? ''
          const all = [
            { id: 'u1', email: 'ada@example.com' },
            { id: 'u2', email: 'bo@example.com' },
          ]
          const items = q ? all.filter((r) => r.email.toLowerCase().includes(q)) : all
          return HttpResponse.json({ items, total: items.length, limit: 100, offset: 0 })
        },
      ),
      http.post('http://localhost/api/v1/reviews/bulk', async ({ request }) => {
        const b = (await request.json()) as {
          rows: { row_key: string; data: { reviewer_id: string } }[]
        }
        return HttpResponse.json({
          dry_run: false,
          total: b.rows.length,
          succeeded: 0,
          failed: b.rows.length,
          results: b.rows.map((r) => ({
            row_key: r.row_key,
            status: 'failed',
            data: null,
            error: { title: 'Conflict', detail: 'already assigned', status: 409 },
          })),
        })
      }),
    )
    const u = userEvent.setup()
    renderWithProviders(
      <AssignReviewerDialog
        flags={[{ id: FLAG_ID, label: 'reason' }]}
        open
        onOpenChange={() => {}}
      />,
    )

    const input = await screen.findByLabelText('Reviewers')
    await u.click(input)
    await u.click(await screen.findByRole('option', { name: /ada@example\.com/ })) // pick ada (broad page)
    await u.type(input, 'bo') // search shifts to another term after ada was already picked
    await screen.findByRole('option', { name: /bo@example\.com/ }) // wait for the re-query to settle
    await u.click(screen.getByRole('button', { name: /^Assign/ }))

    const summary = await screen.findByText(/1 failed/i)
    // ada was picked under the earlier search; her email is still resolved in the summary because the
    // union pool accumulates id→email across searches (never a raw UUID).
    expect(summary.parentElement?.textContent).toMatch(/ada@example\.com/)
  })
})

describe('AssignReviewerDialog — reviewer workload', () => {
  it('shows how loaded each candidate is next to their address', async () => {
    server.use(
      poolEndpoint(FLAG_ID, [
        { id: 'u1', email: 'ada@example.com', active_review_count: 2 },
        { id: 'u2', email: 'bob@example.com', active_review_count: 0 },
      ]),
    )
    const u = userEvent.setup()
    renderWithProviders(
      <AssignReviewerDialog
        flags={[{ id: FLAG_ID, label: 'reason' }]}
        open
        onOpenChange={() => {}}
      />,
    )

    await u.click(await screen.findByLabelText('Reviewers'))

    // The exact accessible name, not a substring of it: the point of putting the count in the
    // option is that a screen reader reads it out, and an `aria-label` added later would silently
    // take it away while a substring match kept passing.
    expect(
      await screen.findByRole('option', { name: 'ada@example.com 2 active reviews' }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('option', { name: 'bob@example.com 0 active reviews' }),
    ).toBeInTheDocument()
  })

  it('keeps the count out of the chip, which is an identity and not a status', async () => {
    server.use(
      poolEndpoint(FLAG_ID, [{ id: 'u1', email: 'ada@example.com', active_review_count: 2 }]),
    )
    const u = userEvent.setup()
    renderWithProviders(
      <AssignReviewerDialog
        flags={[{ id: FLAG_ID, label: 'reason' }]}
        open
        onOpenChange={() => {}}
      />,
    )

    await u.click(await screen.findByLabelText('Reviewers'))
    await u.click(await screen.findByRole('option', { name: /ada@example\.com/ }))

    // The chip wrapper, not its ✕ button — the button carries no text of its own, so asserting on it
    // would pass no matter what the chip said.
    const chip = screen.getByRole('button', { name: /remove ada@example\.com/i }).parentElement
    expect(chip).toHaveTextContent('ada@example.com')
    expect(chip).not.toHaveTextContent('active')
  })
})
