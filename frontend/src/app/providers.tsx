import type { ReactNode } from 'react'
import { QueryClientProvider } from '@tanstack/react-query'
import { ReactQueryDevtools } from '@tanstack/react-query-devtools'
import { Toaster } from 'sonner'
import { queryClient } from '@/lib/query'
import { AuthProvider } from '@/lib/auth/auth-provider'
import { ExportsProvider } from '@/features/exports/exports-store'

export function AppProviders({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <ExportsProvider>{children}</ExportsProvider>
      </AuthProvider>
      <Toaster richColors position="top-right" theme="system" />
      <ReactQueryDevtools initialIsOpen={false} />
    </QueryClientProvider>
  )
}
