import { useCallback, useState } from 'react'
import type { SavedViewState } from '@/lib/api/types'

// Compares with === — assumes primitive filter values (every list's
// defaultFilters holds only strings/booleans). An object/array value would never
// compare equal and would pin isDirty true, so keep filter values primitive.
function sameFilters(a: Record<string, unknown>, b: Record<string, unknown>): boolean {
  const keys = new Set([...Object.keys(a), ...Object.keys(b)])
  return [...keys].every((k) => a[k] === b[k])
}

// One list's captured state: committed search term, per-list filter values (an
// opaque object — the backend never interprets it), the sort token, hidden
// column ids, and the pagination offset. `searchDraft` mirrors the search box
// so the two-state (draft → commit-on-submit) pattern lives in one place.
//
// A named saved view serializes/restores everything EXCEPT the offset — a preset
// is a filter/sort/columns choice, not a page position, so `apply()` resets it.
export type ListViewState<F extends Record<string, unknown>> = {
  search: string
  searchDraft: string
  filters: F
  orderBy: string | undefined
  hiddenColumns: string[]
  offset: number
  // True when search/filters/sort/hidden-columns differ from the defaults —
  // i.e. there is something for a "Reset" control to clear. Ignores offset.
  isDirty: boolean
  setSearchDraft: (value: string) => void
  commitSearch: () => void
  setFilter: <K extends keyof F>(key: K, value: F[K]) => void
  setOrderBy: (value: string) => void
  setOffset: (value: number) => void
  setHiddenColumns: (ids: string[]) => void
  serialize: () => SavedViewState
  apply: (state: SavedViewState) => void
  reset: () => void
}

// `defaultFilters` MUST be a stable reference (declare it at module scope) — it
// seeds the filters and is the fallback for apply()/reset().
export function useListViewState<F extends Record<string, unknown>>({
  defaultFilters,
  defaultOrderBy,
}: {
  defaultFilters: F
  defaultOrderBy?: string
}): ListViewState<F> {
  const [search, setSearch] = useState('')
  const [searchDraft, setSearchDraft] = useState('')
  const [filters, setFilters] = useState<F>(defaultFilters)
  const [orderBy, setOrderByValue] = useState<string | undefined>(defaultOrderBy)
  const [hiddenColumns, setHiddenColumns] = useState<string[]>([])
  const [offset, setOffset] = useState(0)

  const commitSearch = useCallback(() => {
    setSearch(searchDraft.trim())
    setOffset(0)
  }, [searchDraft])

  const setFilter = useCallback(<K extends keyof F>(key: K, value: F[K]) => {
    setFilters((prev) => ({ ...prev, [key]: value }))
    setOffset(0)
  }, [])

  const setOrderBy = useCallback((value: string) => {
    setOrderByValue(value)
    setOffset(0)
  }, [])

  const serialize = useCallback(
    (): SavedViewState => ({
      order_by: orderBy ?? null,
      filters,
      hidden_columns: hiddenColumns,
      search: search.trim() || null,
    }),
    [orderBy, filters, hiddenColumns, search],
  )

  const apply = useCallback(
    (state: SavedViewState) => {
      const nextSearch = state.search ?? ''
      setSearch(nextSearch)
      setSearchDraft(nextSearch)
      // Merge over defaults so a view written before a filter existed still fills
      // every key; unknown stored keys are harmless (the query ignores them).
      setFilters({ ...defaultFilters, ...(state.filters as Partial<F>) } as F)
      setOrderByValue(state.order_by ?? defaultOrderBy)
      setHiddenColumns(state.hidden_columns ?? [])
      setOffset(0)
    },
    [defaultFilters, defaultOrderBy],
  )

  const reset = useCallback(() => {
    setSearch('')
    setSearchDraft('')
    setFilters(defaultFilters)
    setOrderByValue(defaultOrderBy)
    setHiddenColumns([])
    setOffset(0)
  }, [defaultFilters, defaultOrderBy])

  const isDirty =
    search.trim() !== '' ||
    orderBy !== defaultOrderBy ||
    hiddenColumns.length > 0 ||
    !sameFilters(filters, defaultFilters)

  return {
    search,
    searchDraft,
    filters,
    orderBy,
    hiddenColumns,
    offset,
    isDirty,
    setSearchDraft,
    commitSearch,
    setFilter,
    setOrderBy,
    setOffset,
    setHiddenColumns,
    serialize,
    apply,
    reset,
  }
}
