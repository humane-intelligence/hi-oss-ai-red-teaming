import { useLayoutEffect, useRef } from 'react'

// Whether the caller is still mounted, for a write that opted out of the global error toast: an
// inline message written into an unmounted surface reports the failure to nobody, so the caller
// falls back to a toast. Re-armed in the effect body, not just cleared on unmount, so StrictMode's
// mount/unmount/remount cycle does not leave it stuck at `false`.
export function useReachable() {
  const reachable = useRef(true)
  useLayoutEffect(() => {
    reachable.current = true
    return () => {
      reachable.current = false
    }
  }, [])
  return reachable
}
