import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { BulkInviteDialog } from './bulk-invite-dialog'
import type { AssignableRoleResponse, BulkGroupInviteResponse } from '@/lib/api/types'

const GROUP_ID = 'grp-0001-0000-0000-000000000000'
const BULK_URL = `http://localhost/api/v1/evaluation-groups/${GROUP_ID}/invitations/bulk`

const ROLE_TESTER: AssignableRoleResponse = {
  id: 'role-tester',
  name: 'red_teamer',
  display_name: 'Red Teamer',
  is_participant_default: false,
}

// The group lists what is assignable in it; the platform admin role is not, so the server
// leaves it out and the picker never has to know which roles those are.
function rolesHandler() {
  return http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}/assignable-roles`, () =>
    HttpResponse.json({ items: [ROLE_TESTER], total: 1, limit: 100, offset: 0 }),
  )
}

function okRow(row_key: string, email: string, outcome: 'assigned' | 'invited') {
  return {
    row_key,
    status: 'ok' as const,
    data: { outcome, user_id: `u-${row_key}`, email, roles: [ROLE_TESTER], expires_at: null },
  }
}

function renderDialog() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(['evaluation_groups:manage_members'])
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <BulkInviteDialog groupId={GROUP_ID} open onOpenChange={() => {}} />
      </QueryClientProvider>
    </Wrapper>,
  )
}

describe('evaluation-groups BulkInviteDialog — submit', () => {
  it('sends 2 rows with correct emails + role_ids + dry_run:false', async () => {
    const user = userEvent.setup()
    const captured: unknown[] = []

    server.use(
      rolesHandler(),
      http.post(BULK_URL, async ({ request }) => {
        captured.push(await request.json())
        const response: BulkGroupInviteResponse = {
          dry_run: false,
          total: 2,
          succeeded: 2,
          failed: 0,
          results: [
            okRow('0', 'ada@example.com', 'invited'),
            okRow('1', 'bob@example.com', 'assigned'),
          ],
        }
        return HttpResponse.json(response)
      }),
    )

    renderDialog()

    await waitFor(() => expect(screen.getByText('Red Teamer')).toBeInTheDocument())
    await user.type(screen.getByLabelText(/emails/i), 'ada@example.com\nbob@example.com')
    await user.click(screen.getByRole('checkbox', { name: /red teamer/i }))
    await user.click(screen.getByRole('button', { name: /send/i }))

    await waitFor(() => expect(captured).toHaveLength(1))

    const body = captured[0] as {
      rows: { row_key: string; data: { email: string; role_ids: string[] } }[]
      dry_run: boolean
    }
    expect(body.dry_run).toBe(false)
    expect(body.rows).toHaveLength(2)
    expect(body.rows[0]?.data.email).toBe('ada@example.com')
    expect(body.rows[1]?.data.email).toBe('bob@example.com')
    expect(body.rows[0]?.data.role_ids).toContain('role-tester')
    expect(body.rows[0]?.row_key).toBe('0')
    expect(body.rows[1]?.row_key).toBe('1')
  })

  it('shows succeeded count in summary after success', async () => {
    const user = userEvent.setup()

    server.use(
      rolesHandler(),
      http.post(BULK_URL, async () => {
        const response: BulkGroupInviteResponse = {
          dry_run: false,
          total: 2,
          succeeded: 2,
          failed: 0,
          results: [
            okRow('0', 'ada@example.com', 'invited'),
            okRow('1', 'bob@example.com', 'assigned'),
          ],
        }
        return HttpResponse.json(response)
      }),
    )

    renderDialog()

    await waitFor(() => expect(screen.getByText('Red Teamer')).toBeInTheDocument())
    await user.type(screen.getByLabelText(/emails/i), 'ada@example.com\nbob@example.com')
    await user.click(screen.getByRole('checkbox', { name: /red teamer/i }))
    await user.click(screen.getByRole('button', { name: /send/i }))

    await waitFor(() => expect(screen.getByText(/2 of 2 succeeded/i)).toBeInTheDocument())
  })

  it('shows failed email + reason in summary when a row fails', async () => {
    const user = userEvent.setup()

    server.use(
      rolesHandler(),
      http.post(BULK_URL, async () => {
        const response: BulkGroupInviteResponse = {
          dry_run: false,
          total: 2,
          succeeded: 1,
          failed: 1,
          results: [
            okRow('0', 'ada@example.com', 'invited'),
            {
              row_key: '1',
              status: 'failed',
              error: {
                type: 'about:blank',
                title: 'Conflict',
                status: 409,
                detail: 'Already a member',
              },
            },
          ],
        }
        return HttpResponse.json(response)
      }),
    )

    renderDialog()

    await waitFor(() => expect(screen.getByText('Red Teamer')).toBeInTheDocument())
    await user.type(screen.getByLabelText(/emails/i), 'ada@example.com\nbob@example.com')
    await user.click(screen.getByRole('checkbox', { name: /red teamer/i }))
    await user.click(screen.getByRole('button', { name: /send/i }))

    await waitFor(() => expect(screen.getByText(/1 failed/i)).toBeInTheDocument())
    expect(screen.getByText('bob@example.com')).toBeInTheDocument()
    expect(screen.getByText(/Already a member/i)).toBeInTheDocument()
  })
})

describe('evaluation-groups BulkInviteDialog — role picker', () => {
  it('offers the roles the group lists as assignable, never reading the catalog', async () => {
    // The default handler serves Red Teamer from `/roles` too, so the absence of a role cannot
    // show which list the picker read; the flag does.
    let catalogRead = false
    server.use(
      http.get('http://localhost/api/v1/roles', () => {
        catalogRead = true
        return HttpResponse.json({ title: 'Forbidden', status: 403 }, { status: 403 })
      }),
      rolesHandler(),
    )
    renderDialog()

    await waitFor(() =>
      expect(screen.getByRole('checkbox', { name: /red teamer/i })).toBeInTheDocument(),
    )
    expect(catalogRead).toBe(false)
  })
})

describe('evaluation-groups BulkInviteDialog — validation + dedup', () => {
  it('blocks submit and shows an error when an email is malformed (no request sent)', async () => {
    const user = userEvent.setup()
    const captured: unknown[] = []

    server.use(
      rolesHandler(),
      http.post(BULK_URL, async ({ request }) => {
        captured.push(await request.json())
        return HttpResponse.json({ dry_run: false, total: 0, succeeded: 0, failed: 0, results: [] })
      }),
    )

    renderDialog()

    await waitFor(() => expect(screen.getByText('Red Teamer')).toBeInTheDocument())
    await user.type(screen.getByLabelText(/emails/i), 'notanemail')
    await user.click(screen.getByRole('checkbox', { name: /red teamer/i }))
    await user.click(screen.getByRole('button', { name: /send/i }))

    await waitFor(() => expect(screen.getByText(/enter valid emails/i)).toBeInTheDocument())
    expect(captured).toHaveLength(0)
  })

  it('accepts semicolon- and space-separated emails', async () => {
    const user = userEvent.setup()
    const captured: unknown[] = []

    server.use(
      rolesHandler(),
      http.post(BULK_URL, async ({ request }) => {
        captured.push(await request.json())
        return HttpResponse.json({
          dry_run: false,
          total: 3,
          succeeded: 3,
          failed: 0,
          results: [
            okRow('0', 'ada@example.com', 'invited'),
            okRow('1', 'bob@example.com', 'invited'),
            okRow('2', 'carol@example.com', 'invited'),
          ],
        })
      }),
    )

    renderDialog()

    await waitFor(() => expect(screen.getByText('Red Teamer')).toBeInTheDocument())
    await user.type(
      screen.getByLabelText(/emails/i),
      'ada@example.com; bob@example.com carol@example.com',
    )
    await user.click(screen.getByRole('checkbox', { name: /red teamer/i }))
    await user.click(screen.getByRole('button', { name: /send/i }))

    await waitFor(() => expect(captured).toHaveLength(1))
    const body = captured[0] as { rows: { data: { email: string } }[] }
    expect(body.rows.map((r) => r.data.email)).toEqual([
      'ada@example.com',
      'bob@example.com',
      'carol@example.com',
    ])
  })

  it('surfaces an envelope-level 422 on the email field instead of failing silently', async () => {
    const user = userEvent.setup()

    server.use(
      rolesHandler(),
      http.post(BULK_URL, async () =>
        HttpResponse.json(
          {
            type: 'about:blank',
            title: 'Unprocessable Entity',
            status: 422,
            errors: [
              {
                loc: ['body', 'rows'],
                msg: 'List should have at most 100 items after validation, not 101',
                type: 'too_long',
              },
            ],
          },
          { status: 422, headers: { 'content-type': 'application/problem+json' } },
        ),
      ),
    )

    renderDialog()

    await waitFor(() => expect(screen.getByText('Red Teamer')).toBeInTheDocument())
    await user.type(screen.getByLabelText(/emails/i), 'ada@example.com')
    await user.click(screen.getByRole('checkbox', { name: /red teamer/i }))
    await user.click(screen.getByRole('button', { name: /send/i }))

    await waitFor(() => expect(screen.getByText(/at most 100 items/i)).toBeInTheDocument())
  })

  it('does not render a non-field server error inline (the global toast owns it)', async () => {
    const user = userEvent.setup()

    server.use(
      rolesHandler(),
      http.post(BULK_URL, async () =>
        HttpResponse.json(
          { type: 'about:blank', title: 'Internal Server Error', status: 500 },
          { status: 500 },
        ),
      ),
    )

    renderDialog()

    await waitFor(() => expect(screen.getByText('Red Teamer')).toBeInTheDocument())
    await user.type(screen.getByLabelText(/emails/i), 'ada@example.com')
    await user.click(screen.getByRole('checkbox', { name: /red teamer/i }))
    await user.click(screen.getByRole('button', { name: /send/i }))

    await waitFor(() => expect(screen.getByRole('button', { name: /send/i })).toBeEnabled())
    expect(screen.queryByText(/something went wrong/i)).toBeNull()
    expect(screen.getByLabelText(/emails/i)).toHaveValue('ada@example.com')
  })

  it('collapses case-variant duplicates into a single lowercased row', async () => {
    const user = userEvent.setup()
    const captured: unknown[] = []

    server.use(
      rolesHandler(),
      http.post(BULK_URL, async ({ request }) => {
        captured.push(await request.json())
        return HttpResponse.json({
          dry_run: false,
          total: 1,
          succeeded: 1,
          failed: 0,
          results: [okRow('0', 'ada@example.com', 'invited')],
        })
      }),
    )

    renderDialog()

    await waitFor(() => expect(screen.getByText('Red Teamer')).toBeInTheDocument())
    await user.type(screen.getByLabelText(/emails/i), 'Ada@example.com\nada@example.com')
    await user.click(screen.getByRole('checkbox', { name: /red teamer/i }))
    await user.click(screen.getByRole('button', { name: /send/i }))

    await waitFor(() => expect(captured).toHaveLength(1))
    const body = captured[0] as { rows: { data: { email: string } }[] }
    expect(body.rows).toHaveLength(1)
    expect(body.rows[0]?.data.email).toBe('ada@example.com')
  })
})
