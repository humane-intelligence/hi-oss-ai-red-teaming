import { useEffect } from 'react'

// Warn before reloading/closing the tab while a form has unsaved edits.
// In-app (SPA) navigation isn't blocked here — that needs a data-router
// `useBlocker`, which our MemoryRouter-based tests can't host yet.
export function useUnsavedGuard(when: boolean) {
  useEffect(() => {
    if (!when) return
    const handler = (e: BeforeUnloadEvent) => {
      e.preventDefault()
      e.returnValue = ''
    }
    window.addEventListener('beforeunload', handler)
    return () => window.removeEventListener('beforeunload', handler)
  }, [when])
}
