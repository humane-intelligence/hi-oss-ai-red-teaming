import { http, HttpResponse } from 'msw'
import type { PublicPlatformSettingsResponse } from '@/lib/api/types'

// The public settings with only the given fields changed from the test default. Open signup is the
// default so only invite-only tests need to override. `min_length` is deliberately NOT the shipped 8
// that `SHIPPED_PASSWORD_POLICY` falls back to: identical values would make a test unable to tell a
// real read from a failed one.
export function publicSettingsHandler(overrides: Partial<PublicPlatformSettingsResponse>) {
  return http.get('http://localhost/api/v1/platform-settings/public', () =>
    HttpResponse.json({
      signup_enabled: true,
      password_policy: {
        min_length: 10,
        require_uppercase: false,
        require_digit: false,
        require_symbol: false,
      },
      conversation_tags_enabled: true,
      logo_light_key: null,
      logo_dark_key: null,
      ...overrides,
    }),
  )
}
