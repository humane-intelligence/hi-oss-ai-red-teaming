import { StrictMode, type ReactElement } from 'react'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { AuthProvider } from '@/lib/auth/auth-provider'

export function renderWithProviders(
  ui: ReactElement,
  { route = '/', strict = false }: { route?: string; strict?: boolean } = {},
) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const tree = (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>
  )
  // `strict`: mounts under StrictMode's synchronous mount→cleanup→remount cycle, to catch
  // an effect whose cleanup silently disarms a later async callback (write-note-dialog.tsx
  // and edit-tags-dialog.tsx both carry a fix for exactly this).
  return render(strict ? <StrictMode>{tree}</StrictMode> : tree)
}
