import { useEffect } from 'react'
import { useSearchParams } from 'react-router-dom'

// Tab selection in the query string, so a tab is linkable and survives a reload and Back.
//
// `vocabulary` is the page's FULL set of tab values, not the currently visible subset. Visibility
// depends on authority that arrives with an async fetch, and validating against the visible set
// would delete a legitimate `?tab=exports` on the first render — before the permission resolves —
// so the deep link would lose the race every time. Callers therefore validate against the static
// vocabulary here and decide separately which tab they can actually render.
//
// An unrecognised value is dropped with `replace`, not `push`: a hand-typed `?tab=nonsense` must
// not sit in the history stack, or Back walks the user back into the broken URL. Switching tabs
// pushes, so Back moves between tabs the way a user expects. The default tab carries no param at
// all, keeping the canonical URL of a page free of query noise.
//
// `fallback` is explicit rather than `vocabulary[0]`: under `noUncheckedIndexedAccess` an index
// read is `string | undefined`.
export function useTabParam(
  vocabulary: readonly string[],
  fallback: string,
  param = 'tab',
): [string, (next: string) => void] {
  const [searchParams, setSearchParams] = useSearchParams()
  const raw = searchParams.get(param)
  const recognised = raw !== null && vocabulary.includes(raw)
  const active = recognised ? raw : fallback

  useEffect(() => {
    if (raw === null || recognised) return
    const next = new URLSearchParams(searchParams)
    next.delete(param)
    setSearchParams(next, { replace: true })
  }, [raw, recognised, param, searchParams, setSearchParams])

  const setActive = (next: string) => {
    const params = new URLSearchParams(searchParams)
    if (next === fallback) params.delete(param)
    else params.set(param, next)
    setSearchParams(params)
  }

  return [active, setActive]
}
