import type { ReactElement } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'
import { Button } from '@/components/ui/button'

const BASE = import.meta.env.VITE_API_BASE_URL || ''

/** Google's four-colour "G". Decorative — the button's text carries the provider name. */
function GoogleMark() {
  return (
    <svg viewBox="0 0 18 18" className="size-4" aria-hidden="true" focusable="false">
      <path
        fill="#4285F4"
        d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.92a8.78 8.78 0 0 0 2.68-6.62Z"
      />
      <path
        fill="#34A853"
        d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.92-2.26c-.81.54-1.84.86-3.04.86a5.36 5.36 0 0 1-5.03-3.7H1.05v2.34A8.99 8.99 0 0 0 9 18Z"
      />
      <path
        fill="#FBBC05"
        d="M3.97 10.72a5.4 5.4 0 0 1 0-3.44V4.94H1.05a9 9 0 0 0 0 8.12l2.92-2.34Z"
      />
      <path
        fill="#EA4335"
        d="M9 3.58c1.32 0 2.5.45 3.44 1.35l2.58-2.59C13.46.9 11.42 0 9 0A8.99 8.99 0 0 0 1.05 4.94l2.92 2.34A5.36 5.36 0 0 1 9 3.58Z"
      />
    </svg>
  )
}

const MARKS: Record<string, () => ReactElement> = { google: GoogleMark }

export function OidcButtons() {
  const { data: providers } = useQuery({
    queryKey: ['oidc-providers'],
    queryFn: async () => unwrap(await apiClient.GET('/api/v1/auth/oidc/providers')),
  })

  if (!providers?.length) return null

  return (
    <div className="mt-4 space-y-2 border-t pt-4">
      {providers.map((p) => {
        const Mark = MARKS[p]
        return (
          <Button
            key={p}
            type="button"
            variant="outline"
            className="w-full"
            onClick={() =>
              window.location.assign(`${BASE}/api/v1/auth/oidc/${encodeURIComponent(p)}/login`)
            }
          >
            {Mark && <Mark />}
            Continue with <span className="capitalize">{p}</span>
          </Button>
        )
      })}
    </div>
  )
}
