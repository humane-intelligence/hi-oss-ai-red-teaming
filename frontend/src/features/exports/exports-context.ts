import { createContext, useContext } from 'react'

export type ExportsContextValue = {
  // Register a freshly created job so the app polls it in the background and toasts on completion.
  trackExport: (job: { id: string; template: string }, filenameStem: string) => void
  activeCount: number
}

export const ExportsContext = createContext<ExportsContextValue | null>(null)

export function useExports(): ExportsContextValue {
  const ctx = useContext(ExportsContext)
  if (!ctx) throw new Error('useExports must be used within ExportsProvider')
  return ctx
}
