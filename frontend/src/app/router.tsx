import { lazy, Suspense, type ReactElement } from 'react'
import { createBrowserRouter, RouterProvider } from 'react-router-dom'
import { AppShell } from '@/app/layout/app-shell'
import { DetailSkeleton } from '@/components/shared/detail-skeleton'
import { RequireAuth } from '@/lib/auth/require-auth'
import { RequireTermsAcceptance } from '@/features/terms/terms-gate'
import { RequirePermission } from '@/lib/auth/require-permission'
import { RequireRole } from '@/lib/auth/require-role'
import { LoginPage } from '@/features/auth/login-page'
// Eager, like `LoginPage`: this page's whole job is deleting a bearer token pair from
// the URL as fast as possible, so it can't sit behind a chunk-fetch delay.
import { OidcCallbackPage } from '@/features/auth/oidc-callback-page'

const RegisterPage = lazy(() =>
  import('@/features/auth/register-page').then((m) => ({ default: m.RegisterPage })),
)
const VerifyEmailPage = lazy(() =>
  import('@/features/auth/verify-email-page').then((m) => ({ default: m.VerifyEmailPage })),
)
const PasswordResetRequestPage = lazy(() =>
  import('@/features/auth/password-reset-request-page').then((m) => ({
    default: m.PasswordResetRequestPage,
  })),
)
const PasswordResetConfirmPage = lazy(() =>
  import('@/features/auth/password-reset-confirm-page').then((m) => ({
    default: m.PasswordResetConfirmPage,
  })),
)
const AcceptInvitePage = lazy(() =>
  import('@/features/auth/accept-invite-page').then((m) => ({ default: m.AcceptInvitePage })),
)
const OverviewPage = lazy(() =>
  import('@/features/overview/overview-page').then((m) => ({ default: m.OverviewPage })),
)
const AccountPage = lazy(() =>
  import('@/features/account/account-page').then((m) => ({ default: m.AccountPage })),
)
const BackendStatusPage = lazy(() =>
  import('@/features/status/backend-status').then((m) => ({ default: m.BackendStatusPage })),
)
const SystemPreferencesPage = lazy(() =>
  import('@/features/system-preferences/system-preferences-page').then((m) => ({
    default: m.SystemPreferencesPage,
  })),
)
const EvaluationsListPage = lazy(() =>
  import('@/features/evaluations/evaluations-list-page').then((m) => ({
    default: m.EvaluationsListPage,
  })),
)
const EvaluationDetailPage = lazy(() =>
  import('@/features/evaluations/evaluation-detail-page').then((m) => ({
    default: m.EvaluationDetailPage,
  })),
)
const EvaluationFormPage = lazy(() =>
  import('@/features/evaluations/evaluation-form-page').then((m) => ({
    default: m.EvaluationFormPage,
  })),
)
const ConversationDetailPage = lazy(() =>
  import('@/features/conversations/conversation-detail-page').then((m) => ({
    default: m.ConversationDetailPage,
  })),
)
const ConversationGroupsListPage = lazy(() =>
  import('@/features/conversations/conversation-groups-list-page').then((m) => ({
    default: m.ConversationGroupsListPage,
  })),
)
const GroupCreatePage = lazy(() =>
  import('@/features/conversations/group-create-page').then((m) => ({
    default: m.GroupCreatePage,
  })),
)
const ConversationGroupPage = lazy(() =>
  import('@/features/conversations/conversation-group-page').then((m) => ({
    default: m.ConversationGroupPage,
  })),
)
const EvaluationGroupsListPage = lazy(() =>
  import('@/features/evaluation-groups/evaluation-groups-list-page').then((m) => ({
    default: m.EvaluationGroupsListPage,
  })),
)
const EvaluationGroupDetailPage = lazy(() =>
  import('@/features/evaluation-groups/evaluation-group-detail-page').then((m) => ({
    default: m.EvaluationGroupDetailPage,
  })),
)
const EvaluationGroupFormPage = lazy(() =>
  import('@/features/evaluation-groups/evaluation-group-form-page').then((m) => ({
    default: m.EvaluationGroupFormPage,
  })),
)
const AiModelsListPage = lazy(() =>
  import('@/features/ai-models/ai-models-list-page').then((m) => ({ default: m.AiModelsListPage })),
)
const AiModelDetailPage = lazy(() =>
  import('@/features/ai-models/ai-model-detail-page').then((m) => ({
    default: m.AiModelDetailPage,
  })),
)
const AiModelFormPage = lazy(() =>
  import('@/features/ai-models/ai-model-form-page').then((m) => ({ default: m.AiModelFormPage })),
)
const UsersListPage = lazy(() =>
  import('@/features/users/users-list-page').then((m) => ({ default: m.UsersListPage })),
)
const UserEditPage = lazy(() =>
  import('@/features/users/user-edit-page').then((m) => ({ default: m.UserEditPage })),
)
const RolesListPage = lazy(() =>
  import('@/features/roles/roles-list-page').then((m) => ({ default: m.RolesListPage })),
)
const RoleFormPage = lazy(() =>
  import('@/features/roles/role-form-page').then((m) => ({ default: m.RoleFormPage })),
)
const AuditLogsListPage = lazy(() =>
  import('@/features/audit-logs/audit-logs-list-page').then((m) => ({
    default: m.AuditLogsListPage,
  })),
)
const NotificationsListPage = lazy(() =>
  import('@/features/notifications/notifications-list-page').then((m) => ({
    default: m.NotificationsListPage,
  })),
)
const OrganizationsListPage = lazy(() =>
  import('@/features/organizations/organizations-list-page').then((m) => ({
    default: m.OrganizationsListPage,
  })),
)
const OrganizationDetailPage = lazy(() =>
  import('@/features/organizations/organization-detail-page').then((m) => ({
    default: m.OrganizationDetailPage,
  })),
)
const OrganizationFormPage = lazy(() =>
  import('@/features/organizations/organization-form-page').then((m) => ({
    default: m.OrganizationFormPage,
  })),
)
const LicensesListPage = lazy(() =>
  import('@/features/licenses/licenses-list-page').then((m) => ({ default: m.LicensesListPage })),
)
const LicenseDetailPage = lazy(() =>
  import('@/features/licenses/license-detail-page').then((m) => ({ default: m.LicenseDetailPage })),
)
const LicenseFormPage = lazy(() =>
  import('@/features/licenses/license-form-page').then((m) => ({ default: m.LicenseFormPage })),
)
const MessageFlagsListPage = lazy(() =>
  import('@/features/message-flags/message-flags-list-page').then((m) => ({
    default: m.MessageFlagsListPage,
  })),
)
const MessageFlagDetailPage = lazy(() =>
  import('@/features/message-flags/message-flag-detail-page').then((m) => ({
    default: m.MessageFlagDetailPage,
  })),
)
const ReviewsLayout = lazy(() =>
  import('@/features/reviews/reviews-layout').then((m) => ({ default: m.ReviewsLayout })),
)
const ReviewsIndexRedirect = lazy(() =>
  import('@/features/reviews/reviews-layout').then((m) => ({ default: m.ReviewsIndexRedirect })),
)
const MyReviewsPage = lazy(() =>
  import('@/features/reviews/my-reviews-page').then((m) => ({ default: m.MyReviewsPage })),
)
const ReviewQueuePage = lazy(() =>
  import('@/features/reviews/review-queue-page').then((m) => ({ default: m.ReviewQueuePage })),
)
const ReviewsListPage = lazy(() =>
  import('@/features/reviews/reviews-list-page').then((m) => ({ default: m.ReviewsListPage })),
)
const ReviewDetailPage = lazy(() =>
  import('@/features/reviews/review-detail-page').then((m) => ({ default: m.ReviewDetailPage })),
)

const fallback = (
  <div className="p-6">
    <DetailSkeleton />
  </div>
)

function ws(el: ReactElement) {
  return <Suspense fallback={fallback}>{el}</Suspense>
}

// Exported only so `router.test.tsx` can exercise the real route tree instead of a
// hand-built stand-in — the rule can't tell that apart from a genuine fast-refresh hazard.
// eslint-disable-next-line react-refresh/only-export-components
export const router = createBrowserRouter([
  { path: '/login', element: <LoginPage /> },
  { path: '/register', element: ws(<RegisterPage />) },
  { path: '/register/verify', element: ws(<VerifyEmailPage />) },
  { path: '/password-reset', element: ws(<PasswordResetRequestPage />) },
  { path: '/password-reset/confirm', element: ws(<PasswordResetConfirmPage />) },
  { path: '/invite/accept', element: ws(<AcceptInvitePage />) },
  // Where the backend's OIDC callback lands the browser, tokens in the fragment. Must
  // stay a sibling of the `RequireAuth` subtree below, not a child of it: the browser
  // lands here with no token adopted yet, so nesting it under the guard would redirect
  // to `/login` before this page's own effect ever reads the fragment, dropping the
  // token. Locked in by `router.test.tsx`'s test against this exported router, not a
  // hand-built one — see that file for why the guard alone doesn't catch this.
  { path: '/auth/callback', element: <OidcCallbackPage /> },
  {
    element: <RequireAuth />,
    children: [
      {
        // Between the auth guard and the shell: an account owing a terms acceptance gets the gate
        // instead of the application, so no route below it renders or fetches. A route declared as
        // a *sibling* of this one would not be gated — `router.test.tsx` pins the nesting.
        element: <RequireTermsAcceptance />,
        children: [
          {
            element: <AppShell />,
            children: [
              { index: true, element: ws(<OverviewPage />) },
              // No permission gate: every authenticated caller owns their account.
              { path: 'account', element: ws(<AccountPage />) },
              {
                path: 'evaluations',
                element: (
                  <RequirePermission anyOf={['evaluations:read']}>
                    <EvaluationsListPage />
                  </RequirePermission>
                ),
              },
              {
                // Coarse gate only: creating an evaluation is authorized per parent group
                // (an in-group role, not the global permission), so the form enforces
                // `evaluations:create` from the chosen group's `user_permissions`.
                path: 'evaluations/new',
                element: (
                  <RequirePermission anyOf={['evaluations:read']}>
                    <EvaluationFormPage />
                  </RequirePermission>
                ),
              },
              {
                path: 'evaluations/:id',
                element: (
                  <RequirePermission anyOf={['evaluations:read']}>
                    <EvaluationDetailPage />
                  </RequirePermission>
                ),
              },
              {
                path: 'evaluations/:id/edit',
                element: (
                  <RequirePermission anyOf={['evaluations:update']}>
                    <EvaluationFormPage />
                  </RequirePermission>
                ),
              },
              {
                // Coarse gate only, like `conversation-groups/new` below: read authority is
                // per parent group, so the page enforces `conversations:read` once the group
                // resolves. Gating on the global permission here bounced the callers this
                // route exists for — an in-group red teamer lands here right after creating.
                path: 'evaluations/:id/conversations/:conversationId',
                element: (
                  <RequirePermission anyOf={['evaluations:read']}>
                    <ConversationDetailPage />
                  </RequirePermission>
                ),
              },
              {
                // Coarse gate only: starting a conversation is authorized per parent group (an
                // in-group role, not the global permission), so the page enforces
                // `conversations:create` from the group's `user_permissions`. Gating here on a
                // conversation permission would lock out the very callers whose authority is
                // in-group — a group-scoped red teamer whose global role grants none.
                path: 'evaluations/:id/conversation-groups/new',
                element: (
                  <RequirePermission anyOf={['evaluations:read']}>
                    <GroupCreatePage />
                  </RequirePermission>
                ),
              },
              {
                // Coarse gate only — see the conversation detail route above.
                path: 'evaluations/:id/conversation-groups/:groupId',
                element: (
                  <RequirePermission anyOf={['evaluations:read']}>
                    <ConversationGroupPage />
                  </RequirePermission>
                ),
              },
              {
                path: 'conversation-groups',
                element: (
                  <RequirePermission anyOf={['conversations:read']}>
                    <ConversationGroupsListPage />
                  </RequirePermission>
                ),
              },
              {
                path: 'evaluation-groups',
                element: (
                  <RequirePermission anyOf={['evaluation_groups:read']}>
                    <EvaluationGroupsListPage />
                  </RequirePermission>
                ),
              },
              {
                path: 'evaluation-groups/new',
                element: (
                  <RequirePermission anyOf={['evaluation_groups:create']}>
                    <EvaluationGroupFormPage />
                  </RequirePermission>
                ),
              },
              {
                path: 'evaluation-groups/:id',
                element: (
                  <RequirePermission anyOf={['evaluation_groups:read']}>
                    <EvaluationGroupDetailPage />
                  </RequirePermission>
                ),
              },
              {
                // Coarse gate only: edit authority is object-scoped (an in-group role,
                // not the global permission), so the form itself enforces
                // `evaluation_groups:update` from the group's `user_permissions`.
                path: 'evaluation-groups/:id/edit',
                element: (
                  <RequirePermission anyOf={['evaluation_groups:read']}>
                    <EvaluationGroupFormPage />
                  </RequirePermission>
                ),
              },
              {
                path: 'message-flags',
                element: (
                  <RequirePermission anyOf={['flags:read']}>
                    <MessageFlagsListPage />
                  </RequirePermission>
                ),
              },
              {
                // Coarse gate only, like the conversation routes: `require_flag_permission` accepts
                // `flags:read` from a role held on the flag's group, so gating the route on the global
                // key sent an in-group caller to NotAuthorized on a flag the rail had just linked to.
                // The flag fetch itself is the enforcement — the server 403s a caller with neither.
                path: 'message-flags/:id',
                element: (
                  <RequirePermission anyOf={['evaluations:read']}>
                    <MessageFlagDetailPage />
                  </RequirePermission>
                ),
              },
              {
                path: 'reviews',
                element: (
                  <RequirePermission anyOf={['reviews:read']}>
                    <ReviewsLayout />
                  </RequirePermission>
                ),
                children: [
                  { index: true, element: ws(<ReviewsIndexRedirect />) },
                  { path: 'queue', element: ws(<ReviewQueuePage />) },
                  {
                    path: 'mine',
                    element: ws(
                      <RequirePermission anyOf={['reviews:update']}>
                        <MyReviewsPage />
                      </RequirePermission>,
                    ),
                  },
                  { path: 'all', element: ws(<ReviewsListPage />) },
                ],
              },
              {
                path: 'reviews/submissions/:submissionId',
                element: (
                  <RequirePermission anyOf={['reviews:read']}>
                    <ReviewDetailPage />
                  </RequirePermission>
                ),
              },
              {
                path: 'ai-models',
                element: (
                  <RequirePermission anyOf={['models:read']}>
                    <AiModelsListPage />
                  </RequirePermission>
                ),
              },
              {
                path: 'ai-models/new',
                element: (
                  <RequirePermission anyOf={['models:create']}>
                    <AiModelFormPage />
                  </RequirePermission>
                ),
              },
              {
                path: 'ai-models/:id',
                element: (
                  <RequirePermission anyOf={['models:read']}>
                    <AiModelDetailPage />
                  </RequirePermission>
                ),
              },
              {
                path: 'ai-models/:id/edit',
                element: (
                  <RequirePermission anyOf={['models:update']}>
                    <AiModelFormPage />
                  </RequirePermission>
                ),
              },
              {
                path: 'users',
                element: (
                  <RequirePermission anyOf={['users:read']}>
                    <UsersListPage />
                  </RequirePermission>
                ),
              },
              {
                path: 'users/:id/edit',
                element: (
                  <RequirePermission anyOf={['users:update']}>
                    <UserEditPage />
                  </RequirePermission>
                ),
              },
              {
                path: 'roles',
                element: (
                  <RequirePermission anyOf={['roles:read']}>
                    <RolesListPage />
                  </RequirePermission>
                ),
              },
              {
                path: 'roles/new',
                element: (
                  <RequirePermission anyOf={['roles:manage']}>
                    <RoleFormPage />
                  </RequirePermission>
                ),
              },
              {
                path: 'roles/:id/edit',
                element: (
                  <RequirePermission anyOf={['roles:manage']}>
                    <RoleFormPage />
                  </RequirePermission>
                ),
              },
              {
                path: 'audit-logs',
                element: (
                  <RequirePermission anyOf={['audit:read']}>
                    <AuditLogsListPage />
                  </RequirePermission>
                ),
              },
              {
                path: 'notifications',
                element: (
                  <RequirePermission anyOf={['notifications:read']}>
                    <NotificationsListPage />
                  </RequirePermission>
                ),
              },
              {
                path: 'organizations',
                element: (
                  <RequirePermission anyOf={['organizations:read']}>
                    <OrganizationsListPage />
                  </RequirePermission>
                ),
              },
              {
                path: 'organizations/new',
                element: (
                  <RequirePermission anyOf={['organizations:create']}>
                    <OrganizationFormPage />
                  </RequirePermission>
                ),
              },
              {
                path: 'organizations/:id',
                element: (
                  <RequirePermission anyOf={['organizations:read']}>
                    <OrganizationDetailPage />
                  </RequirePermission>
                ),
              },
              {
                path: 'organizations/:id/edit',
                element: (
                  <RequirePermission anyOf={['organizations:update']}>
                    <OrganizationFormPage />
                  </RequirePermission>
                ),
              },
              {
                path: 'licenses',
                element: (
                  <RequirePermission
                    anyOf={['licenses:create', 'licenses:update', 'licenses:delete']}
                  >
                    <LicensesListPage />
                  </RequirePermission>
                ),
              },
              {
                path: 'licenses/new',
                element: (
                  <RequirePermission anyOf={['licenses:create']}>
                    <LicenseFormPage />
                  </RequirePermission>
                ),
              },
              {
                path: 'licenses/:id',
                element: (
                  <RequirePermission
                    anyOf={['licenses:create', 'licenses:update', 'licenses:delete']}
                  >
                    <LicenseDetailPage />
                  </RequirePermission>
                ),
              },
              {
                path: 'licenses/:id/edit',
                element: (
                  <RequirePermission anyOf={['licenses:update']}>
                    <LicenseFormPage />
                  </RequirePermission>
                ),
              },
              {
                path: 'system-preferences',
                element: (
                  <RequirePermission anyOf={['platform_settings:read']}>
                    <SystemPreferencesPage />
                  </RequirePermission>
                ),
              },
              {
                path: 'status',
                element: (
                  <RequireRole anyOf={['admin']}>
                    <BackendStatusPage />
                  </RequireRole>
                ),
              },
            ],
          },
        ],
      },
    ],
  },
])

export function AppRouter() {
  return <RouterProvider router={router} />
}
