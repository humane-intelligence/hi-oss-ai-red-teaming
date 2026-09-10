import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { BulkInviteDialog } from './bulk-invite-dialog'
import type { BulkGroupInviteResponse, RoleResponse } from '@/lib/api/types'

const GROUP_ID = 'grp-0001-0000-0000-000000000000'
const BULK_URL = `http://localhost/api/v1/evaluation-groups/${GROUP_ID}/invitations/bulk`

const ROLE_TESTER: RoleResponse = {
  id: 'role-tester',
  name: 'red_teamer',
  display_name: 'Red Teamer',
  permissions: [],
  is_system: true,
  is_active: true,
  is_default: false,
  is_participant_default: false,
  is_object_assignable: true,
}

const ROLE_ADMIN: RoleResponse = {
  id: 'role-admin',
  name: 'admin',
  display_name: 'Admin',
  permissions: [],
  is_system: true,
  is_active: true,
  is_default: false,
  is_participant_default: false,
  is_object_assignable: false,
}

// Stands in for the endpoint's `is_object_assignable` filter — the in-group picker asks
// for that subset rather than filtering client-side.
function rolesHandler() {
  const items = [ROLE_TESTER, ROLE_ADMIN]
  return http.get('http://localhost/api/v1/roles', ({ request }) => {
    const param = new URL(request.url).searchParams.get('is_object_assignable')
    const filtered =
      param === null ? items : items.filter((r) => r.is_object_assignable === (param === 'true'))
    return HttpResponse.json({ items: filtered, total: filtered.length, limit: 100, offset: 0 })
  })
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
  it('omits the platform admin role (not assignable in a group)', async () => {
    server.use(rolesHandler())
    renderDialog()

    await waitFor(() =>
      expect(screen.getByRole('checkbox', { name: /red teamer/i })).toBeInTheDocument(),
    )
    expect(screen.queryByRole('checkbox', { name: /admin/i })).toBeNull()
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
