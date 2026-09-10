import { StrictMode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { MAX_INVITE_ROWS } from '@/lib/api/limits'
import { InviteDialog, RoleChips } from './invite-dialog'
import type { BulkInviteResponse, RoleResponse } from '@/lib/api/types'

const ROLE_VIEWER: RoleResponse = {
  id: 'role-viewer',
  name: 'viewer',
  display_name: 'Viewer',
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

const OK_ROW = (key: string, email: string) => ({
  row_key: key,
  status: 'ok' as const,
  data: {
    id: `inv-${key}`,
    user_id: `u${key}`,
    email,
    status: 'pending' as const,
    expires_at: '',
    created_at: '',
  },
})

function rolesHandler() {
  return http.get('http://localhost/api/v1/roles', () =>
    HttpResponse.json({ items: [ROLE_VIEWER, ROLE_ADMIN], total: 2, limit: 100, offset: 0 }),
  )
}

// StrictMode like main.tsx: its double render is what surfaces form state surviving a close —
// without it the reopen regressions below pass against the broken version too.
function renderDialog() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(['users:invite'])
  // Spied rather than wired to setOpen: closing from inside a click handler would re-enter
  // rerender() and trip React's act warning. The tests drive open/closed themselves.
  const onOpenChange = vi.fn()
  const tree = (open: boolean) => (
    <StrictMode>
      <Wrapper>
        <QueryClientProvider client={qc}>
          <InviteDialog open={open} onOpenChange={onOpenChange} />
        </QueryClientProvider>
      </Wrapper>
    </StrictMode>
  )
  const view = render(tree(true))
  const setOpen = (open: boolean) => view.rerender(tree(open))
  return { setOpen, onOpenChange }
}

function rowRoles(index: number) {
  return within(screen.getByRole('group', { name: `Invitee ${index} roles` }))
}

describe('InviteDialog — submit', () => {
  it('sends per-row emails and roles with dry_run:false', async () => {
    const user = userEvent.setup()
    const captured: unknown[] = []

    server.use(
      rolesHandler(),
      http.post('http://localhost/api/v1/auth/invitations/bulk', async ({ request }) => {
        captured.push(await request.json())
        const response: BulkInviteResponse = {
          dry_run: false,
          total: 2,
          succeeded: 2,
          failed: 0,
          results: [OK_ROW('0', 'alice@example.com'), OK_ROW('1', 'bob@example.com')],
        }
        return HttpResponse.json(response)
      }),
    )

    renderDialog()
    await waitFor(() =>
      expect(rowRoles(1).getByRole('button', { name: 'Viewer' })).toBeInTheDocument(),
    )

    await user.type(screen.getByLabelText('Invitee 1 email'), 'alice@example.com')
    await user.click(rowRoles(1).getByRole('button', { name: 'Viewer' }))
    await user.click(screen.getByRole('button', { name: /add row/i }))
    await user.type(screen.getByLabelText('Invitee 2 email'), 'bob@example.com')
    // The new row copied row 1's roles; make row 2 differ to prove roles are per-row.
    await user.click(rowRoles(2).getByRole('button', { name: 'Viewer' }))
    await user.click(rowRoles(2).getByRole('button', { name: 'Admin' }))
    await user.click(screen.getByRole('button', { name: /send 2 invites/i }))

    await waitFor(() => expect(captured).toHaveLength(1))
    expect(captured[0]).toEqual({
      dry_run: false,
      rows: [
        { row_key: '0', data: { email: 'alice@example.com', role_ids: ['role-viewer'] } },
        { row_key: '1', data: { email: 'bob@example.com', role_ids: ['role-admin'] } },
      ],
    })
  })

  it('shows the summary after success and the failed row by its email and detail', async () => {
    const user = userEvent.setup()

    server.use(
      rolesHandler(),
      http.post('http://localhost/api/v1/auth/invitations/bulk', () => {
        const response: BulkInviteResponse = {
          dry_run: false,
          total: 2,
          succeeded: 1,
          failed: 1,
          results: [
            OK_ROW('0', 'alice@example.com'),
            {
              row_key: '1',
              status: 'failed',
              data: null,
              error: {
                type: 'about:blank',
                title: 'Conflict',
                status: 409,
                detail: 'Email already invited',
              },
            },
          ],
        }
        return HttpResponse.json(response)
      }),
    )

    renderDialog()
    await waitFor(() =>
      expect(rowRoles(1).getByRole('button', { name: 'Viewer' })).toBeInTheDocument(),
    )

    await user.type(screen.getByLabelText('Invitee 1 email'), 'alice@example.com')
    await user.click(rowRoles(1).getByRole('button', { name: 'Viewer' }))
    await user.click(screen.getByRole('button', { name: /add row/i }))
    await user.type(screen.getByLabelText('Invitee 2 email'), 'bob@example.com')
    await user.click(screen.getByRole('button', { name: /send 2 invites/i }))

    await waitFor(() => expect(screen.getByText(/1 of 2 succeeded/i)).toBeInTheDocument())
    // The failed row shows the request-side email and the problem detail, not the generic title.
    const failedRow = screen.getByRole('listitem')
    expect(failedRow).toHaveTextContent('bob@example.com: Email already invited')
    expect(failedRow).not.toHaveTextContent('Conflict')
  })
})

describe('InviteDialog — client guards', () => {
  it('flags a case-variant duplicate on its row and sends nothing', async () => {
    const user = userEvent.setup()
    let calls = 0

    server.use(
      rolesHandler(),
      http.post('http://localhost/api/v1/auth/invitations/bulk', () => {
        calls += 1
        return HttpResponse.json({ dry_run: false, total: 0, succeeded: 0, failed: 0, results: [] })
      }),
    )

    renderDialog()
    await waitFor(() =>
      expect(rowRoles(1).getByRole('button', { name: 'Viewer' })).toBeInTheDocument(),
    )

    await user.type(screen.getByLabelText('Invitee 1 email'), 'ada@example.com')
    await user.click(rowRoles(1).getByRole('button', { name: 'Viewer' }))
    await user.click(screen.getByRole('button', { name: /add row/i }))
    await user.type(screen.getByLabelText('Invitee 2 email'), 'Ada@example.com')
    await user.click(screen.getByRole('button', { name: /send 2 invites/i }))

    expect(await screen.findByText('Duplicate of row 1')).toBeInTheDocument()
    expect(calls).toBe(0)
  })

  it('blocks a pasted list over the row cap before it reaches the server', async () => {
    const user = userEvent.setup()
    let calls = 0

    server.use(
      rolesHandler(),
      http.post('http://localhost/api/v1/auth/invitations/bulk', () => {
        calls += 1
        return HttpResponse.json({ dry_run: false, total: 0, succeeded: 0, failed: 0, results: [] })
      }),
    )

    renderDialog()
    await waitFor(() =>
      expect(rowRoles(1).getByRole('button', { name: 'Viewer' })).toBeInTheDocument(),
    )

    await user.click(screen.getByRole('button', { name: /paste list/i }))
    await user.click(screen.getByLabelText(/paste emails/i))
    await user.paste(
      Array.from({ length: MAX_INVITE_ROWS + 1 }, (_, i) => `u${i}@example.com`).join('\n'),
    )
    await user.click(screen.getByRole('button', { name: /add rows/i }))

    expect(
      screen.getByText(`${MAX_INVITE_ROWS + 1} of ${MAX_INVITE_ROWS} row(s)`),
    ).toBeInTheDocument()

    await user.click(
      screen.getByRole('button', { name: new RegExp(`send ${MAX_INVITE_ROWS + 1} invites`, 'i') }),
    )

    expect(
      await screen.findByText(new RegExp(`at most ${MAX_INVITE_ROWS} invitees`, 'i')),
    ).toBeInTheDocument()
    expect(calls).toBe(0)
  })

  it('surfaces an envelope-level 422 inline instead of failing silently', async () => {
    const user = userEvent.setup()

    server.use(
      rolesHandler(),
      http.post('http://localhost/api/v1/auth/invitations/bulk', () =>
        HttpResponse.json(
          {
            type: 'about:blank',
            title: 'Unprocessable Content',
            status: 422,
            errors: [
              { loc: ['body', 'rows'], msg: 'Duplicate emails in request', type: 'value_error' },
            ],
          },
          { status: 422, headers: { 'content-type': 'application/problem+json' } },
        ),
      ),
    )

    renderDialog()
    await waitFor(() =>
      expect(rowRoles(1).getByRole('button', { name: 'Viewer' })).toBeInTheDocument(),
    )

    await user.type(screen.getByLabelText('Invitee 1 email'), 'ada@example.com')
    await user.click(rowRoles(1).getByRole('button', { name: 'Viewer' }))
    await user.click(screen.getByRole('button', { name: /send 1 invite/i }))

    expect(await screen.findByText(/duplicate emails in request/i)).toBeInTheDocument()
  })
})

function uploadCsv(content: string) {
  const file = new File([content], 'invitees.csv', { type: 'text/csv' })
  const input = document.querySelector('input[type="file"]') as HTMLInputElement
  fireEvent.change(input, { target: { files: [file] } })
}

describe('InviteDialog — CSV upload', () => {
  it('fills rows from a two-column file, matching roles case-insensitively', async () => {
    server.use(rolesHandler())
    renderDialog()
    await waitFor(() =>
      expect(rowRoles(1).getByRole('button', { name: 'Viewer' })).toBeInTheDocument(),
    )

    // The upload control is a real button — reachable by keyboard, unlike a label-wrapped input.
    expect(screen.getByRole('button', { name: /upload csv/i })).toBeInTheDocument()

    uploadCsv('email,role\nBob@example.com,viewer\ncarol@example.com,Admin\n')

    await waitFor(() =>
      expect(screen.getByLabelText('Invitee 2 email')).toHaveValue('carol@example.com'),
    )
    expect(screen.getByLabelText('Invitee 1 email')).toHaveValue('bob@example.com')
    expect(rowRoles(1).getByRole('button', { name: 'Viewer' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(rowRoles(2).getByRole('button', { name: 'Admin' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(screen.getByText('2 of 100 row(s)')).toBeInTheDocument()
  })

  it('rejects the whole file and lists parse errors per line', async () => {
    server.use(rolesHandler())
    renderDialog()
    await waitFor(() =>
      expect(rowRoles(1).getByRole('button', { name: 'Viewer' })).toBeInTheDocument(),
    )

    uploadCsv(
      'not-an-email,viewer\nbob@example.com,superuser\nbob@example.com\ncarol@example.com,viewer\n',
    )

    expect(await screen.findByText(/file not imported/i)).toBeInTheDocument()
    expect(screen.getByText('Line 1: invalid email "not-an-email"')).toBeInTheDocument()
    expect(screen.getByText('Line 2: unknown role "superuser"')).toBeInTheDocument()
    expect(
      screen.getByText('Line 3: expected two columns (email, role), got 1'),
    ).toBeInTheDocument()
    // All-or-nothing: the one valid line must not have been imported.
    expect(screen.queryByLabelText('Invitee 2 email')).toBeNull()
    expect(screen.getByLabelText('Invitee 1 email')).toHaveValue('')
  })

  it('flags an in-file duplicate and clears the errors on a fixed re-upload', async () => {
    server.use(rolesHandler())
    renderDialog()
    await waitFor(() =>
      expect(rowRoles(1).getByRole('button', { name: 'Viewer' })).toBeInTheDocument(),
    )

    uploadCsv('ada@example.com,viewer\nAda@example.com,admin\n')
    expect(await screen.findByText('Line 2: duplicate email "ada@example.com"')).toBeInTheDocument()

    uploadCsv('ada@example.com,viewer\n')
    await waitFor(() =>
      expect(screen.getByLabelText('Invitee 1 email')).toHaveValue('ada@example.com'),
    )
    expect(screen.queryByText(/file not imported/i)).toBeNull()
  })
})

describe('RoleChips', () => {
  // The deterministic pin for the browser-only crash: a field-array replace() can hand the
  // Controller one undefined-valued render before RHF settles.
  it('renders unpressed chips when the value is undefined', async () => {
    server.use(rolesHandler())
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const Wrapper = authWrapper(['users:invite'])
    render(
      <Wrapper>
        <QueryClientProvider client={qc}>
          <RoleChips value={undefined} onChange={() => {}} label="row roles" />
        </QueryClientProvider>
      </Wrapper>,
    )

    expect(await screen.findByRole('button', { name: 'Viewer' })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
  })
})

describe('InviteDialog — reopen after success', () => {
  // In a real browser this sequence handed the role Controller an undefined value and crashed
  // (jsdom doesn't reproduce the timing — the RoleChips guard is what holds the line there);
  // this pins the intended behavior of the cycle: clean row on reopen, paste-add keeps working.
  it('survives close → reopen → paste-add and starts from a clean single row', async () => {
    const user = userEvent.setup()

    server.use(
      rolesHandler(),
      http.post('http://localhost/api/v1/auth/invitations/bulk', () => {
        const response: BulkInviteResponse = {
          dry_run: false,
          total: 1,
          succeeded: 1,
          failed: 0,
          results: [OK_ROW('0', 'ada@example.com')],
        }
        return HttpResponse.json(response)
      }),
    )

    const { setOpen, onOpenChange } = renderDialog()
    await waitFor(() =>
      expect(rowRoles(1).getByRole('button', { name: 'Viewer' })).toBeInTheDocument(),
    )

    await user.type(screen.getByLabelText('Invitee 1 email'), 'ada@example.com')
    await user.click(rowRoles(1).getByRole('button', { name: 'Viewer' }))
    await user.click(screen.getByRole('button', { name: /send 1 invite/i }))
    await waitFor(() => expect(screen.getByText(/1 of 1 succeeded/i)).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: /^close$/i }))
    expect(onOpenChange).toHaveBeenCalledWith(false)
    setOpen(false)
    setOpen(true)

    await waitFor(() => expect(screen.getByLabelText('Invitee 1 email')).toHaveValue(''))
    await user.type(screen.getByLabelText('Invitee 1 email'), 'lead@example.com')
    await user.click(rowRoles(1).getByRole('button', { name: 'Viewer' }))
    await user.click(screen.getByRole('button', { name: /paste list/i }))
    await user.click(screen.getByLabelText(/paste emails/i))
    await user.paste('pasted-a@example.com pasted-b@example.com')
    await user.click(screen.getByRole('button', { name: /add rows/i }))

    await waitFor(() =>
      expect(screen.getByLabelText('Invitee 3 email')).toHaveValue('pasted-b@example.com'),
    )
    expect(rowRoles(3).getByRole('button', { name: 'Viewer' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
  })
})

describe('InviteDialog — reopen after cancel', () => {
  it('submits after a cancelled multi-row edit', async () => {
    const user = userEvent.setup()
    const captured: unknown[] = []

    server.use(
      rolesHandler(),
      http.post('http://localhost/api/v1/auth/invitations/bulk', async ({ request }) => {
        captured.push(await request.json())
        const response: BulkInviteResponse = {
          dry_run: false,
          total: 1,
          succeeded: 1,
          failed: 0,
          results: [OK_ROW('0', 'ada@example.com')],
        }
        return HttpResponse.json(response)
      }),
    )

    const { setOpen, onOpenChange } = renderDialog()
    await waitFor(() =>
      expect(rowRoles(1).getByRole('button', { name: 'Viewer' })).toBeInTheDocument(),
    )

    await user.type(screen.getByLabelText('Invitee 1 email'), 'first@example.com')
    await user.click(rowRoles(1).getByRole('button', { name: 'Viewer' }))
    await user.click(screen.getByRole('button', { name: /add row/i }))
    await user.type(screen.getByLabelText('Invitee 2 email'), 'second@example.com')
    await user.click(screen.getByRole('button', { name: /^cancel$/i }))
    expect(onOpenChange).toHaveBeenCalledWith(false)
    setOpen(false)
    setOpen(true)

    await waitFor(() => expect(screen.getByLabelText('Invitee 1 email')).toHaveValue(''))
    expect(screen.queryByLabelText('Invitee 2 email')).not.toBeInTheDocument()

    await user.type(screen.getByLabelText('Invitee 1 email'), 'ada@example.com')
    await user.click(rowRoles(1).getByRole('button', { name: 'Viewer' }))
    await user.click(screen.getByRole('button', { name: /send 1 invite/i }))

    await waitFor(() => expect(captured).toHaveLength(1))
    expect(captured[0]).toEqual({
      dry_run: false,
      rows: [{ row_key: '0', data: { email: 'ada@example.com', role_ids: ['role-viewer'] } }],
    })
  })

  it('re-imports the same CSV after a cancelled import, and drops the parse errors', async () => {
    const user = userEvent.setup()
    server.use(rolesHandler())
    const GOOD = 'csv-a@example.com,viewer\ncsv-b@example.com,viewer\n'

    const { setOpen } = renderDialog()
    await waitFor(() =>
      expect(rowRoles(1).getByRole('button', { name: 'Viewer' })).toBeInTheDocument(),
    )

    // Parse errors are the other state the removed reset-on-open used to clear.
    uploadCsv('not-an-email,viewer\n')
    expect(await screen.findByText(/File not imported/i)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /^cancel$/i }))
    setOpen(false)
    setOpen(true)
    expect(screen.queryByText(/File not imported/i)).toBeNull()

    uploadCsv(GOOD)
    await waitFor(() =>
      expect(screen.getByLabelText('Invitee 2 email')).toHaveValue('csv-b@example.com'),
    )

    await user.click(screen.getByRole('button', { name: /^cancel$/i }))
    setOpen(false)
    setOpen(true)
    await waitFor(() => expect(screen.getByLabelText('Invitee 1 email')).toHaveValue(''))

    uploadCsv(GOOD)

    await waitFor(() =>
      expect(screen.getByLabelText('Invitee 2 email')).toHaveValue('csv-b@example.com'),
    )
    expect(screen.queryByText(/File not imported/i)).not.toBeInTheDocument()
  })
})

describe('InviteDialog — paste list', () => {
  it('appends deduped rows that copy the roles of the last row', async () => {
    const user = userEvent.setup()
    const captured: unknown[] = []

    server.use(
      rolesHandler(),
      http.post('http://localhost/api/v1/auth/invitations/bulk', async ({ request }) => {
        captured.push(await request.json())
        const response: BulkInviteResponse = {
          dry_run: false,
          total: 3,
          succeeded: 3,
          failed: 0,
          results: [
            OK_ROW('0', 'ada@example.com'),
            OK_ROW('1', 'bob@example.com'),
            OK_ROW('2', 'carol@example.com'),
          ],
        }
        return HttpResponse.json(response)
      }),
    )

    renderDialog()
    await waitFor(() =>
      expect(rowRoles(1).getByRole('button', { name: 'Viewer' })).toBeInTheDocument(),
    )

    await user.type(screen.getByLabelText('Invitee 1 email'), 'ada@example.com')
    await user.click(rowRoles(1).getByRole('button', { name: 'Viewer' }))
    await user.click(screen.getByRole('button', { name: /paste list/i }))
    await user.click(screen.getByLabelText(/paste emails/i))
    await user.paste('Bob@example.com bob@example.com, carol@example.com')
    await user.click(screen.getByRole('button', { name: /add rows/i }))

    expect(screen.getByLabelText('Invitee 3 email')).toHaveValue('carol@example.com')
    await user.click(screen.getByRole('button', { name: /send 3 invites/i }))

    await waitFor(() => expect(captured).toHaveLength(1))
    const body = captured[0] as { rows: { data: { email: string; role_ids: string[] } }[] }
    expect(body.rows.map((r) => r.data.email)).toEqual([
      'ada@example.com',
      'bob@example.com',
      'carol@example.com',
    ])
    expect(body.rows.every((r) => r.data.role_ids.includes('role-viewer'))).toBe(true)
  })
})
