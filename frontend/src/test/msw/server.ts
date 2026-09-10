import { setupServer } from 'msw/node'
import { http, HttpResponse } from 'msw'

// AuthShell and the status page fetch /version on every render; a default keeps
// tests that don't care about it from tripping onUnhandledRequest: 'error'.
// Survives resetHandlers() (part of the initial set); override per-test with server.use().
const defaultHandlers = [
  http.get('http://localhost/version', () =>
    HttpResponse.json({ version: 'test', environment: 'dev' }),
  ),
  // The login/register screens read the public signup flag on every render; open signup is
  // the default so only invite-only tests need to override. `min_length` is deliberately NOT the
  // shipped 8 that `SHIPPED_PASSWORD_POLICY` falls back to (and that the pending state used to
  // yield): identical values would make a test unable to tell a real read from a failed one.
  http.get('http://localhost/api/v1/platform-settings/public', () =>
    HttpResponse.json({
      signup_enabled: true,
      password_policy: {
        min_length: 10,
        require_uppercase: false,
        require_digit: false,
        require_symbol: false,
      },
    }),
  ),
  // The signup screens, the account page and the acceptance gate all read the current terms.
  // "Nothing published" (404) is the shipped state and what `useCurrentTerms` maps to `null`, so
  // this default keeps every test that predates consent behaving exactly as before.
  http.get('http://localhost/api/v1/terms/current', () =>
    HttpResponse.json(
      {
        title: 'Not Found',
        status: 404,
        detail: 'The platform has no published terms of service.',
      },
      { status: 404 },
    ),
  ),
  // The AI-model form reads the label vocabulary on every render; an empty set is the default,
  // so only tests that care about suggestions need to override it.
  http.get('http://localhost/api/v1/ai-models/labels', () => HttpResponse.json([])),
  // The export dialog resolves the red_teamer role id via useRoles on every open;
  // a default keeps tests that don't care about it from tripping onUnhandledRequest.
  http.get('http://localhost/api/v1/roles', () =>
    HttpResponse.json({
      items: [
        {
          id: 'role-red-teamer',
          name: 'red_teamer',
          display_name: 'Red Teamer',
          permissions: [],
          is_system: true,
          is_active: true,
          is_default: true,
          is_participant_default: true,
          is_object_assignable: true,
        },
      ],
      total: 1,
      limit: 100,
      offset: 0,
    }),
  ),
]

export const server = setupServer(...defaultHandlers)
