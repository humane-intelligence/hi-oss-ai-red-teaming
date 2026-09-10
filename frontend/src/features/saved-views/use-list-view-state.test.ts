import { describe, expect, it } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { useListViewState } from './use-list-view-state'

type Filters = { status: string; role: string }
const DEFAULT_FILTERS: Filters = { status: '', role: '' }

function setup() {
  return renderHook(() =>
    useListViewState<Filters>({ defaultFilters: DEFAULT_FILTERS, defaultOrderBy: '-created_at' }),
  )
}

describe('useListViewState', () => {
  it('starts at defaults', () => {
    const { result } = setup()
    expect(result.current.search).toBe('')
    expect(result.current.filters).toEqual(DEFAULT_FILTERS)
    expect(result.current.orderBy).toBe('-created_at')
    expect(result.current.hiddenColumns).toEqual([])
    expect(result.current.offset).toBe(0)
  })

  it('commitSearch trims the draft and resets offset', () => {
    const { result } = setup()
    act(() => result.current.setOffset(40))
    act(() => result.current.setSearchDraft('  hello  '))
    act(() => result.current.commitSearch())
    expect(result.current.search).toBe('hello')
    expect(result.current.offset).toBe(0)
  })

  it('setFilter updates one key and resets offset', () => {
    const { result } = setup()
    act(() => result.current.setOffset(20))
    act(() => result.current.setFilter('status', 'published'))
    expect(result.current.filters).toEqual({ status: 'published', role: '' })
    expect(result.current.offset).toBe(0)
  })

  it('setOrderBy updates the token and resets offset', () => {
    const { result } = setup()
    act(() => result.current.setOffset(20))
    act(() => result.current.setOrderBy('title'))
    expect(result.current.orderBy).toBe('title')
    expect(result.current.offset).toBe(0)
  })

  it('serialize captures search/filters/order_by/hidden_columns and omits offset', () => {
    const { result } = setup()
    act(() => result.current.setSearchDraft('term'))
    act(() => result.current.commitSearch())
    act(() => result.current.setFilter('status', 'draft'))
    act(() => result.current.setHiddenColumns(['created_at']))
    const state = result.current.serialize()
    expect(state).toEqual({
      order_by: '-created_at',
      filters: { status: 'draft', role: '' },
      hidden_columns: ['created_at'],
      search: 'term',
    })
    expect(state).not.toHaveProperty('offset')
  })

  it('serialize emits null search when empty', () => {
    const { result } = setup()
    expect(result.current.serialize().search).toBeNull()
  })

  it('apply restores every field, syncs the draft, and resets offset', () => {
    const { result } = setup()
    act(() => result.current.setOffset(60))
    act(() =>
      result.current.apply({
        order_by: 'title',
        filters: { status: 'approved', role: 'admin' },
        hidden_columns: ['group'],
        search: 'restored',
      }),
    )
    expect(result.current.search).toBe('restored')
    expect(result.current.searchDraft).toBe('restored')
    expect(result.current.filters).toEqual({ status: 'approved', role: 'admin' })
    expect(result.current.orderBy).toBe('title')
    expect(result.current.hiddenColumns).toEqual(['group'])
    expect(result.current.offset).toBe(0)
  })

  it('apply fills missing filter keys from defaults and falls back on empty state', () => {
    const { result } = setup()
    act(() => result.current.apply({ filters: { status: 'new' } }))
    expect(result.current.filters).toEqual({ status: 'new', role: '' })
    expect(result.current.orderBy).toBe('-created_at')
    expect(result.current.search).toBe('')
    expect(result.current.hiddenColumns).toEqual([])
  })

  it('reset returns to defaults', () => {
    const { result } = setup()
    act(() =>
      result.current.apply({ search: 'x', filters: { status: 'y', role: 'z' }, order_by: 'title' }),
    )
    act(() => result.current.reset())
    expect(result.current.search).toBe('')
    expect(result.current.searchDraft).toBe('')
    expect(result.current.filters).toEqual(DEFAULT_FILTERS)
    expect(result.current.orderBy).toBe('-created_at')
  })

  it('isDirty is false at defaults and true once anything changes', () => {
    const { result } = setup()
    expect(result.current.isDirty).toBe(false)

    act(() => result.current.setFilter('status', 'published'))
    expect(result.current.isDirty).toBe(true)

    act(() => result.current.reset())
    expect(result.current.isDirty).toBe(false)

    act(() => result.current.setHiddenColumns(['created_at']))
    expect(result.current.isDirty).toBe(true)
  })

  it('isDirty tracks search and sort independently of offset', () => {
    const { result } = setup()
    act(() => result.current.setOffset(40))
    expect(result.current.isDirty).toBe(false)

    act(() => result.current.setSearchDraft('term'))
    act(() => result.current.commitSearch())
    expect(result.current.isDirty).toBe(true)

    act(() => result.current.reset())
    act(() => result.current.setOrderBy('title'))
    expect(result.current.isDirty).toBe(true)
  })
})
