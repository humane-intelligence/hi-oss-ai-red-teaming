import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { EvaluationGroupDetailPage } from './evaluation-group-detail-page'
import type { EvaluationGroupDetailResponse } from '@/lib/api/types'
import { licenseStub, noLicenseStub } from '@/features/licenses/test-fixtures'
import { groupMetricsStub } from './test-fixtures'

const GROUP_ID = 'grp-0001-0000-0000-000000000000'

const baseGroup: EvaluationGroupDetailResponse = {
  id: GROUP_ID,
  title: 'Test group',
  description: 'A test group',
  created_by_id: 'user-0001',
  status: 'approved',
  access_level: 'public',
  metrics_access_during: 'owner_only',
  metrics_access_after: 'owner_only',
  start_date: '2026-01-01',
  data_license_id: null,
  effective_license: licenseStub('CC-BY-4.0'),
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  evaluations: [],
  user_permissions: [],
  publication_blockers: [],
}

const publishedGroup: EvaluationGroupDetailResponse = {
  ...baseGroup,
  status: 'published',
}

const pendingApprovalGroup: EvaluationGroupDetailResponse = {
  ...baseGroup,
  status: 'pending_approval',
}

const publishedOrgGroup: EvaluationGroupDetailResponse = {
  ...baseGroup,
  status: 'published',
  access_level: 'organization',
  organization_id: 'org-0001',
}

const publishedInvitationOnlyGroup: EvaluationGroupDetailResponse = {
  ...baseGroup,
  status: 'published',
  access_level: 'invitation_only',
}

// A complete draft — the server reports no blockers, so Submit is enabled.
const draftGroup: EvaluationGroupDetailResponse = {
  ...baseGroup,
  status: 'draft',
}

// A thin draft saved with only a title. The gaps are the server's own sentences —
// the page renders them verbatim rather than re-deriving them from the fields.
const incompleteDraftGroup: EvaluationGroupDetailResponse = {
  ...baseGroup,
  status: 'draft',
  description: null,
  start_date: null,
  publication_blockers: ['Fill in the required fields: description, start date.'],
}

// Approved but empty — the publish gate wants an evaluation before it goes live.
const unpublishableGroup: EvaluationGroupDetailResponse = {
  ...baseGroup,
  publication_blockers: ['Add at least one evaluation to the group.'],
}

// Action gating now follows the caller's effective in-group authority, surfaced
// on the detail response as `user_permissions` — not the global permission set.
function withPerms(
  group: EvaluationGroupDetailResponse,
  userPermissions: string[],
): EvaluationGroupDetailResponse {
  return { ...group, user_permissions: userPermissions }
}

function makeHandlers(group: EvaluationGroupDetailResponse = baseGroup) {
  return [
    http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, () =>
      HttpResponse.json(group),
    ),
    http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}/members`, () =>
      HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
    ),
    http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}/annotators`, () =>
      HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
    ),
    http.get('http://localhost/api/v1/auth/users', () =>
      HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
    ),
    http.get('http://localhost/api/v1/roles', () =>
      HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
    ),
  ]
}

// `permissions` here are the caller's *global* permissions. Only genuinely-global
// capabilities (e.g. `users:read` for creator resolution) read from them now;
// group actions read from the group's `user_permissions` (see `withPerms`).
function renderPage(permissions: string[] = [], entry = `/evaluation-groups/${GROUP_ID}`) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(permissions)
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[entry]}>
          <Routes>
            <Route path="/evaluation-groups/:id/edit" element={<div>EDIT PAGE</div>} />
            <Route path="/evaluation-groups/:id" element={<EvaluationGroupDetailPage />} />
            <Route path="/evaluation-groups" element={<div>Groups list</div>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
  return qc
}

describe('EvaluationGroupDetailPage — RBAC gates', () => {
  it('with in-group read only — no Edit, Publish, Finish, Reject, New evaluation', async () => {
    server.use(...makeHandlers(withPerms(baseGroup, ['evaluation_groups:read'])))
    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    expect(screen.queryByRole('button', { name: /edit/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /publish/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /finish/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /reject/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /new evaluation/i })).toBeNull()
  })

  it('in-group update authority shows Edit + Publish even with no global permissions', async () => {
    // The fix: an in-group owner whose global role lacks evaluation_groups:update
    // still sees the lifecycle actions, because gating reads the group's
    // user_permissions, not the global set (here deliberately empty).
    server.use(...makeHandlers(withPerms(baseGroup, ['evaluation_groups:update'])))
    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: /edit/i })).toBeInTheDocument()
    // Publish/approve/submit/finish are owner-or-manage (update-gated).
    expect(screen.getByRole('button', { name: /publish/i })).toBeInTheDocument()
  })

  it('global update without in-group authority shows nothing (no over-granting)', async () => {
    // A non-member who carries evaluation_groups:update globally has no authority
    // on this group (empty user_permissions), so the server would 403 — the UI
    // hides the actions to match, rather than gating on the global union.
    server.use(...makeHandlers(withPerms(baseGroup, [])))
    // Global perms deliberately carry update + create; with no in-group authority
    // the object-gated buttons must still stay hidden (no over-granting).
    renderPage(['evaluation_groups:update', 'evaluations:create'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    expect(screen.queryByRole('button', { name: /edit/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /publish/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /new evaluation/i })).toBeNull()
  })

  it('with in-group update + status=published — Finish visible, no Publish', async () => {
    server.use(...makeHandlers(withPerms(publishedGroup, ['evaluation_groups:update'])))
    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: /finish/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /publish/i })).toBeNull()
  })

  it('with update + manage + status=pending_approval — Approve, Request changes, Reject visible', async () => {
    server.use(
      ...makeHandlers(
        withPerms(pendingApprovalGroup, ['evaluation_groups:update', 'evaluation_groups:manage']),
      ),
    )
    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: /^approve$/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /request changes/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^reject$/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /publish/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /finish/i })).toBeNull()
  })

  it('status=pending_approval with update but no manage — Approve visible, but not Request changes / Reject', async () => {
    // Approve is owner-or-manage (update-gated); request-changes and reject stay
    // moderator-only (manage), so an owner can approve but not bounce/reject.
    server.use(...makeHandlers(withPerms(pendingApprovalGroup, ['evaluation_groups:update'])))
    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: /^approve$/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /request changes/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /^reject$/i })).toBeNull()
  })

  it('status=pending_approval without update or manage — no Approve, Request changes, Reject', async () => {
    server.use(...makeHandlers(withPerms(pendingApprovalGroup, ['evaluation_groups:read'])))
    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    expect(screen.queryByRole('button', { name: /^approve$/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /request changes/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /^reject$/i })).toBeNull()
  })

  it('Request changes asks for confirmation before bouncing the group', async () => {
    let requested = false
    server.use(
      ...makeHandlers(
        withPerms(pendingApprovalGroup, ['evaluation_groups:update', 'evaluation_groups:manage']),
      ),
      http.post(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}/request-changes`, () => {
        requested = true
        return HttpResponse.json(
          { ...pendingApprovalGroup, status: 'changes_requested' },
          { status: 200 },
        )
      }),
    )
    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    // Header button opens the confirm — it must NOT fire the bounce immediately.
    fireEvent.click(screen.getByRole('button', { name: /request changes/i }))
    expect(requested).toBe(false)
    // Confirm in the dialog.
    fireEvent.click(
      within(screen.getByRole('dialog')).getByRole('button', { name: /request changes/i }),
    )
    await waitFor(() => expect(requested).toBe(true))
  })

  it('with in-group update + status=draft — Submit for approval visible', async () => {
    server.use(...makeHandlers(withPerms(draftGroup, ['evaluation_groups:update'])))
    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: /submit for approval/i })).toBeInTheDocument()
  })

  it('with evaluation_groups:update + complete draft — Submit for approval enabled', async () => {
    server.use(...makeHandlers(withPerms(draftGroup, ['evaluation_groups:update'])))
    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: /submit for approval/i })).toBeEnabled()
  })

  it('incomplete draft — Submit disabled + an inline hint says what to fix', async () => {
    server.use(...makeHandlers(withPerms(incompleteDraftGroup, ['evaluation_groups:update'])))
    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: /submit for approval/i })).toBeDisabled()
    // The page tells the owner what's missing — not just a hover tooltip.
    expect(screen.getByText(/not ready to submit/i)).toBeInTheDocument()
    expect(
      screen.getByText('Fill in the required fields: description, start date.'),
    ).toBeInTheDocument()
  })

  it('approved group with publish blockers — Publish disabled + an inline hint', async () => {
    server.use(...makeHandlers(withPerms(unpublishableGroup, ['evaluation_groups:update'])))
    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: /publish/i })).toBeDisabled()
    expect(screen.getByText(/not ready to publish/i)).toBeInTheDocument()
    expect(screen.getByText('Add at least one evaluation to the group.')).toBeInTheDocument()
    // Editing the group fixes nothing here — the gap is a missing child.
    expect(screen.queryByRole('button', { name: /edit draft/i })).toBeNull()
  })

  it('approved group with no blockers — Publish enabled, no hint', async () => {
    server.use(...makeHandlers(withPerms(baseGroup, ['evaluation_groups:update'])))
    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: /publish/i })).toBeEnabled()
    expect(screen.queryByText(/not ready to/i)).toBeNull()
  })

  it('status=draft without update — no Submit', async () => {
    server.use(...makeHandlers(withPerms(draftGroup, ['evaluation_groups:read'])))
    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    expect(screen.queryByRole('button', { name: /submit for approval/i })).toBeNull()
  })

  it('with in-group update — no Publish when status is not approved', async () => {
    server.use(...makeHandlers(withPerms(publishedGroup, ['evaluation_groups:update'])))
    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    expect(screen.queryByRole('button', { name: /publish/i })).toBeNull()
  })

  it('Join visible regardless of permissions when status=published', async () => {
    server.use(...makeHandlers(withPerms(publishedGroup, [])))
    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: /join/i })).toBeInTheDocument()
  })

  // Not in the actions menu: by construction Join only appears for someone with no other action
  // here, and the rest of `primary` needs `evaluation_groups:update`, so behind the kebab it left a
  // non-member facing an empty primary slot on a phone.
  it('Join is a primary action, not a menu item', async () => {
    server.use(...makeHandlers(withPerms(publishedGroup, [])))
    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    const join = screen.getByRole('button', { name: /join/i })
    expect(join).toBeInTheDocument()
    // An inline secondary action is `hidden sm:inline-flex`; a primary one carries no such class.
    expect(join.className).not.toMatch(/hidden/)
    expect(join.closest('[role="menu"]')).toBeNull()
  })

  it('Join visible for a published organization group', async () => {
    server.use(...makeHandlers(publishedOrgGroup))
    renderPage(['evaluation_groups:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: /join/i })).toBeInTheDocument()
  })

  it('Join not visible for an invitation-only group', async () => {
    server.use(...makeHandlers(publishedInvitationOnlyGroup))
    renderPage(['evaluation_groups:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    expect(screen.queryByRole('button', { name: /join/i })).toBeNull()
  })

  it('Join not visible when status is not published', async () => {
    server.use(...makeHandlers(withPerms(baseGroup, [])))
    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    expect(screen.queryByRole('button', { name: /join/i })).toBeNull()
  })

  it('with in-group evaluations:create — New evaluation button visible', async () => {
    // Object-gated like the lifecycle actions: an in-group role granting
    // evaluations:create shows the entry point even without the global permission.
    server.use(...makeHandlers(withPerms(baseGroup, ['evaluations:create'])))
    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: /new evaluation/i })).toBeInTheDocument()
  })

  it('Join not visible when the caller is already a member', async () => {
    // authWrapper's default user id is '1'; a member list that includes them
    // suppresses the Join button (clicking it would only 409).
    server.use(
      http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}/members`, () =>
        HttpResponse.json({
          items: [
            {
              user: { id: '1', email: 'a@b.c' },
              roles: [{ id: 'rt', name: 'red_teamer', display_name: 'Red Teamer' }],
            },
          ],
          total: 1,
          limit: 100,
          offset: 0,
        }),
      ),
      ...makeHandlers(publishedGroup),
    )
    renderPage(['evaluation_groups:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    expect(screen.queryByRole('button', { name: /join/i })).toBeNull()
  })

  it('with in-group evaluations:create — New evaluation button visible', async () => {
    // Object-gated like the lifecycle actions: an in-group role granting
    // evaluations:create shows the entry point even without the global permission.
    server.use(...makeHandlers(withPerms(baseGroup, ['evaluations:create'])))
    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: /new evaluation/i })).toBeInTheDocument()
  })

  it('non-approved group hides New evaluation despite in-group evaluations:create', async () => {
    // Lifecycle gate, not authority: adding evaluations 409s until the group is
    // approved/published, so the entry point stays hidden on a pending group.
    server.use(...makeHandlers(withPerms(pendingApprovalGroup, ['evaluations:create'])))
    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    expect(screen.queryByRole('button', { name: /new evaluation/i })).toBeNull()
  })

  it('published group still shows New evaluation with in-group evaluations:create', async () => {
    server.use(...makeHandlers(withPerms(publishedGroup, ['evaluations:create'])))
    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: /new evaluation/i })).toBeInTheDocument()
  })

  it('empty evaluations hint explains the approval gate on a non-accepting group', async () => {
    server.use(...makeHandlers(withPerms(pendingApprovalGroup, ['evaluations:create'])))
    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    expect(screen.getByText(/added once the group is approved/i)).toBeInTheDocument()
  })

  it('terminal states get their own hint — no false promise of approval', async () => {
    // A finished group will never become approved; "once the group is approved"
    // would mislead, so the hint says adding is over.
    server.use(
      ...makeHandlers(withPerms({ ...baseGroup, status: 'inactive' }, ['evaluations:create'])),
    )
    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    expect(screen.getByText(/evaluations can no longer be added/i)).toBeInTheDocument()
    expect(screen.queryByText(/once the group is approved/i)).toBeNull()
  })

  it('full in-group authority + appropriate status — all buttons visible', async () => {
    server.use(
      ...makeHandlers(
        withPerms(baseGroup, [
          'evaluation_groups:update',
          'evaluation_groups:manage',
          'evaluations:create',
        ]),
      ),
    )
    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: /edit/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /publish/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /new evaluation/i })).toBeInTheDocument()
  })
})

describe('EvaluationGroupDetailPage — created_by resolution', () => {
  it('shows resolved email when users list includes the creator', async () => {
    const handlers = [
      http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}`, () =>
        HttpResponse.json(baseGroup),
      ),
      http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}/members`, () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
      http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}/annotators`, () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
      http.get('http://localhost/api/v1/auth/users', () =>
        HttpResponse.json({
          items: [{ id: 'user-0001', email: 'alice@example.com' }],
          total: 1,
          limit: 100,
          offset: 0,
        }),
      ),
      http.get('http://localhost/api/v1/roles', () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
    ]
    server.use(...handlers)
    // resolving the creator needs the users list — gated on the global users:read (see useUserLookup)
    renderPage(['users:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    await waitFor(() => expect(screen.getByText('alice@example.com')).toBeInTheDocument())
  })

  it('shows a neutral placeholder when users query returns empty (non-admin persona)', async () => {
    server.use(...makeHandlers())
    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    // non-admin can't read the users list, so the creator stays unresolved — no raw UUID
    await waitFor(() => expect(screen.getByText('unknown user')).toBeInTheDocument())
  })
})

describe('EvaluationGroupDetailPage — draft nulls & duplicate', () => {
  const draftNullGroup: EvaluationGroupDetailResponse = {
    ...baseGroup,
    title: null,
    description: null,
    access_level: 'invitation_only',
    start_date: null,
    status: 'draft',
  }

  it('renders a placeholder for a null draft title', async () => {
    server.use(...makeHandlers(draftNullGroup))
    renderPage(['evaluation_groups:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: /untitled draft/i })).toBeInTheDocument(),
    )
  })

  it('Duplicate opens a dialog, sends include_children when checked, and navigates', async () => {
    let includeChildren: string | null = null
    server.use(
      ...makeHandlers(),
      http.post(
        `http://localhost/api/v1/evaluation-groups/${GROUP_ID}/duplicate`,
        ({ request }) => {
          includeChildren = new URL(request.url).searchParams.get('include_children')
          return HttpResponse.json({ ...baseGroup, id: 'dup-id', status: 'draft' }, { status: 201 })
        },
      ),
    )
    renderPage(['evaluation_groups:read', 'evaluation_groups:create'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    // Header button opens the dialog (no request yet).
    fireEvent.click(screen.getByRole('button', { name: /duplicate/i }))
    fireEvent.click(await screen.findByRole('checkbox'))
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: /duplicate/i }))

    await waitFor(() => expect(includeChildren).toBe('true'))
    await waitFor(() => expect(screen.getByText('EDIT PAGE')).toBeInTheDocument())
  })

  it('Duplicate is hidden without evaluation_groups:create', async () => {
    server.use(...makeHandlers())
    renderPage(['evaluation_groups:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    expect(screen.queryByRole('button', { name: /duplicate/i })).toBeNull()
  })
})

describe('EvaluationGroupDetailPage — metrics tab', () => {
  // A minimal evaluation child so the Evaluations table has a row.
  const evalChild = {
    id: 'eval-1',
    title: 'Prompt injection',
    status: 'draft',
    models: [],
  } as unknown as NonNullable<EvaluationGroupDetailResponse['evaluations']>[number]

  // The typed shared stub, not a hand-written literal: the previous local one had drifted to
  // `participants` (vs `members`) precisely because it was untyped and `tsc` never saw it.
  const metricsWithEval = groupMetricsStub({
    group_id: GROUP_ID,
    evaluations: [
      {
        ...groupMetricsStub().evaluations[0]!,
        evaluation_id: 'eval-1',
        title: 'Prompt injection',
      },
    ],
  })

  // What only a page-level test can cover: the tab is wired to the query and the panel. The
  // panel's own behaviour (error copy, last-good data, empty series) is covered in
  // group-metrics-tab.test.tsx and deliberately not duplicated here.
  it('renders metrics on the Metrics tab, and none on Overview', async () => {
    const group = {
      ...withPerms(baseGroup, ['evaluation_groups:view_metrics']),
      evaluations: [evalChild],
    }
    server.use(
      ...makeHandlers(group),
      http.get(`http://localhost/api/v1/evaluation-groups/${GROUP_ID}/metrics`, () =>
        HttpResponse.json(metricsWithEval),
      ),
    )
    renderPage()

    // Overview carries no metrics at all any more — that is the point of the move.
    await waitFor(() => expect(screen.getByRole('tab', { name: 'Metrics' })).toBeInTheDocument())
    expect(screen.queryByText('Reviews & exploits')).toBeNull()
    expect(screen.queryByText('System-prompt leak')).toBeNull()

    fireEvent.click(screen.getByRole('tab', { name: 'Metrics' }))

    await waitFor(() => expect(screen.getByText('Reviews & exploits')).toBeInTheDocument())
    // The per-evaluation breakdown is a section of the tab now, not a row expander.
    expect(screen.getByText('System-prompt leak')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /expand row/i })).toBeNull()
    expect(screen.queryByText('Participants by role')).toBeNull()
  })

  it('shows no Metrics tab without view-metrics authority', async () => {
    server.use(...makeHandlers(withPerms(baseGroup, ['evaluation_groups:read'])))
    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    expect(screen.queryByRole('tab', { name: 'Metrics' })).toBeNull()
  })
})

describe('EvaluationGroupDetailPage — tabs', () => {
  it('opens on the Exports tab from a deep link', async () => {
    // Export authority is its own permission, not `:update` — editing a group must not confer
    // every member's transcripts, flags and reviews.
    server.use(...makeHandlers(withPerms(baseGroup, ['evaluation_groups:export'])))
    renderPage(['evaluation_groups:read'], `/evaluation-groups/${GROUP_ID}?tab=exports`)

    expect(await screen.findByRole('tab', { name: 'Exports', selected: true })).toBeInTheDocument()
  })

  it('opens on the Metrics tab from a deep link', async () => {
    server.use(...makeHandlers(withPerms(baseGroup, ['evaluation_groups:view_metrics'])))
    renderPage(['evaluation_groups:read'], `/evaluation-groups/${GROUP_ID}?tab=metrics`)

    expect(await screen.findByRole('tab', { name: 'Metrics', selected: true })).toBeInTheDocument()
  })

  it('falls back to Overview when the deep-linked tab is not available to the caller', async () => {
    // The value is legal, so `useTabParam` keeps it in the URL; what the page renders is the
    // separate visible-set decision. Without export authority there is no Exports panel, and
    // the page must show Overview rather than an empty body.
    server.use(...makeHandlers(withPerms(baseGroup, ['evaluation_groups:read'])))
    renderPage(['evaluation_groups:read'], `/evaluation-groups/${GROUP_ID}?tab=exports`)

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    expect(screen.queryByRole('tablist')).toBeNull()
    expect(screen.getByText('A test group')).toBeInTheDocument()
  })

  it('shows no tab bar for a viewer holding neither authority', async () => {
    server.use(...makeHandlers(withPerms(baseGroup, ['evaluation_groups:read'])))
    renderPage(['evaluation_groups:read'])

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    expect(screen.queryByRole('tablist')).toBeNull()
    // …and no panel left pointing at a tab that was never rendered: an orphan `tabpanel` has no
    // accessible name and wraps the whole body in a tab stop.
    expect(screen.queryByRole('tabpanel')).toBeNull()
    expect(document.getElementById('tab-overview')).toBeNull()
  })
})

describe('EvaluationGroupDetailPage — analytics visibility details', () => {
  it('shows the during/after metrics-access levels in the Details card, using the shared labels', async () => {
    const group = {
      ...baseGroup,
      metrics_access_during: 'all_members',
      metrics_access_after: 'inherit_group_access',
    } satisfies EvaluationGroupDetailResponse
    server.use(...makeHandlers(group))
    renderPage()

    await waitFor(() => expect(screen.getByText('Analytics (while active)')).toBeInTheDocument())
    expect(screen.getByText('all members')).toBeInTheDocument()
    expect(screen.getByText('Analytics (after finish)')).toBeInTheDocument()
    expect(screen.getByText('everyone who can see the group')).toBeInTheDocument()
  })
})

describe('EvaluationGroupDetailPage — data license', () => {
  it('shows the effective license with a platform-default hint when there is no override', async () => {
    server.use(...makeHandlers(withPerms(baseGroup, ['evaluation_groups:read'])))
    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    // effective_license resolves the platform default; the catalog links it.
    const link = screen.getByRole('link', { name: 'CC-BY-4.0' })
    expect(link).toHaveAttribute('href', licenseStub('CC-BY-4.0').reference_url)
    expect(screen.getByText('(platform default)')).toBeInTheDocument()
  })

  it('lets a caller with no licenses:* permission read the license text', async () => {
    // The licence pages are gated on `licenses:*`, so this dialog is the only surface where a
    // red-teamer can read the text of the licence covering their data.
    const noLicense = noLicenseStub()
    const closed = { ...baseGroup, data_license_id: noLicense.id, effective_license: noLicense }
    server.use(
      ...makeHandlers(withPerms(closed, ['evaluation_groups:read'])),
      http.get(`http://localhost/api/v1/licenses/${noLicense.id}`, () =>
        HttpResponse.json({ ...noLicense, content: 'NO LICENSE — CLOSED DATA' }),
      ),
    )
    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByRole('button', { name: /no license/i }))

    await waitFor(() => expect(screen.getByText(/CLOSED DATA/)).toBeInTheDocument())
  })

  it('shows the group override without the platform-default hint', async () => {
    const overridden = {
      ...baseGroup,
      data_license_id: licenseStub('CC0-1.0').id,
      effective_license: licenseStub('CC0-1.0'),
    }
    server.use(...makeHandlers(withPerms(overridden, ['evaluation_groups:read'])))
    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    expect(screen.getByRole('link', { name: 'CC0-1.0' })).toBeInTheDocument()
    expect(screen.queryByText('(platform default)')).toBeNull()
  })
})

describe('EvaluationGroupDetailPage — the identity heading', () => {
  it('heads the page with the identity line instead of repeating the title under a breadcrumb', async () => {
    server.use(...makeHandlers(withPerms(baseGroup, ['evaluation_groups:read'])))
    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Test group' })).toBeInTheDocument(),
    )
    // One heading, and the old separate breadcrumb trail is gone with it.
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1)
    expect(screen.queryByRole('navigation', { name: 'Breadcrumb' })).toBeNull()
    expect(screen.getByRole('link', { name: 'Evaluation Groups' })).toHaveAttribute(
      'href',
      '/evaluation-groups',
    )
    // The trail sits above the badge row, not inside it: in the row the badges centre against the
    // whole two-row block and end up beside the crumb rather than the title.
    const trail = screen.getByRole('navigation', { name: 'Evaluation groups list' })
    const heading = screen.getByRole('heading', { level: 1 })
    expect(trail.contains(heading)).toBe(false)
    expect(heading.parentElement?.contains(trail)).toBe(false)
  })
})
