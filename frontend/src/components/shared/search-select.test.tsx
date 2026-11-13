import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { createEvent, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { SearchSelect } from './search-select'

const OPTS = [
  { value: 'u1', label: 'alice@ex.com' },
  { value: 'u2', label: 'bob@ex.com' },
]

function setup(overrides: Partial<Parameters<typeof SearchSelect>[0]> = {}) {
  const onChange = vi.fn()
  const onSearchChange = vi.fn()
  render(
    <SearchSelect
      label="Red-teamer"
      htmlFor="rt"
      value=""
      onChange={onChange}
      onSearchChange={onSearchChange}
      options={OPTS}
      isPending={false}
      isError={false}
      {...overrides}
    />,
  )
  return { onChange, onSearchChange }
}

describe('SearchSelect (typeahead combobox)', () => {
  it('opens the list on typing and reports the debounced term, filtering live', async () => {
    const { onSearchChange } = setup()
    const input = screen.getByLabelText('Red-teamer')

    fireEvent.focus(input)
    fireEvent.change(input, { target: { value: 'ali' } })

    // list auto-appears (no separate dropdown to open), and the term reaches the parent debounced
    expect(screen.getByRole('option', { name: 'alice@ex.com' })).toBeInTheDocument()
    await waitFor(() => expect(onSearchChange).toHaveBeenCalledWith('ali'), { timeout: 800 })
  })

  it('selects an option on click', async () => {
    const { onChange } = setup()
    const u = userEvent.setup()
    await u.click(screen.getByLabelText('Red-teamer'))
    await u.click(screen.getByRole('option', { name: 'bob@ex.com' }))
    expect(onChange).toHaveBeenCalledWith('u2')
  })

  it('supports keyboard nav: ArrowDown then Enter selects the active option', () => {
    const { onChange } = setup()
    const input = screen.getByLabelText('Red-teamer')
    fireEvent.focus(input)
    fireEvent.keyDown(input, { key: 'ArrowDown' }) // → first option
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onChange).toHaveBeenCalledWith('u1')
  })

  it('does not re-search for the label after a selection', async () => {
    const { onSearchChange } = setup()
    const u = userEvent.setup()
    await u.click(screen.getByLabelText('Red-teamer'))
    onSearchChange.mockClear() // ignore the mount/open debounce
    await u.click(screen.getByRole('option', { name: 'bob@ex.com' }))
    await new Promise((r) => setTimeout(r, 400)) // let the debounce window pass
    expect(onSearchChange).not.toHaveBeenCalledWith('bob@ex.com')
  })

  it('shows a disabled option with its hint and ignores a click on it', async () => {
    const { onChange } = setup({
      options: [OPTS[0]!, { value: 'u2', label: 'bob@ex.com', disabled: true, hint: 'Member' }],
    })
    const u = userEvent.setup()
    await u.click(screen.getByLabelText('Red-teamer'))
    const bob = screen.getByRole('option', { name: 'bob@ex.com Member' })
    expect(bob).toHaveAttribute('aria-disabled', 'true')
    await u.click(bob)
    expect(onChange).not.toHaveBeenCalled()
  })

  it('arrow keys skip disabled options', () => {
    const { onChange } = setup({
      options: [
        { value: 'u1', label: 'a', disabled: true },
        { value: 'u2', label: 'b' },
        { value: 'u3', label: 'c', disabled: true },
        { value: 'u4', label: 'd' },
      ],
    })
    const input = screen.getByLabelText('Red-teamer')
    fireEvent.focus(input)
    fireEvent.keyDown(input, { key: 'ArrowDown' }) // → b, past a
    fireEvent.keyDown(input, { key: 'ArrowDown' }) // → d, past c
    fireEvent.keyDown(input, { key: 'ArrowDown' }) // stays on d
    fireEvent.keyDown(input, { key: 'ArrowUp' }) // → b, past c
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onChange).toHaveBeenCalledWith('u2')
  })

  it('scrolls the highlighted option into view', () => {
    const scrollIntoView = vi.spyOn(Element.prototype, 'scrollIntoView')
    setup()
    const input = screen.getByLabelText('Red-teamer')
    fireEvent.focus(input)
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    expect(scrollIntoView.mock.contexts.at(-1)).toBe(
      screen.getByRole('option', { name: 'bob@ex.com' }),
    )
    expect(scrollIntoView).toHaveBeenLastCalledWith({ block: 'nearest' })
    scrollIntoView.mockRestore()
  })

  it('prevents Enter from submitting the form while the list is open', () => {
    setup()
    const input = screen.getByLabelText('Red-teamer')
    fireEvent.focus(input) // open, nothing highlighted (active = -1)
    const enter = createEvent.keyDown(input, { key: 'Enter' })
    fireEvent(input, enter)
    expect(enter.defaultPrevented).toBe(true)
  })

  it('Escape closes the list', () => {
    setup()
    const input = screen.getByLabelText('Red-teamer')
    fireEvent.focus(input)
    expect(screen.getByRole('listbox')).toBeInTheDocument()
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
  })

  // A native <dialog> (showModal) closes on an un-swallowed Esc, wiping the export form. jsdom does
  // not emulate that dialog cancel, so a Modal-mounted test would false-green — instead assert the
  // fix's mechanism directly: while open, Esc is preventDefault'd (cancels the dialog default) and
  // stopPropagation'd (a parent/dialog keydown handler never sees it).
  it('swallows Escape while the list is open so a surrounding dialog stays open', () => {
    const parentKeyDown = vi.fn()
    render(
      <div onKeyDown={parentKeyDown}>
        <SearchSelect
          label="Reviewer"
          htmlFor="rv"
          value=""
          onChange={() => {}}
          onSearchChange={() => {}}
          options={OPTS}
          isPending={false}
          isError={false}
        />
      </div>,
    )
    const input = screen.getByLabelText('Reviewer')
    fireEvent.focus(input)
    expect(screen.getByRole('listbox')).toBeInTheDocument()
    const esc = createEvent.keyDown(input, { key: 'Escape' })
    fireEvent(input, esc)
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(esc.defaultPrevented).toBe(true)
    expect(parentKeyDown).not.toHaveBeenCalled()
  })

  it('lets Escape bubble when the list is already closed (so it can close the surrounding dialog)', () => {
    const parentKeyDown = vi.fn()
    render(
      <div onKeyDown={parentKeyDown}>
        <SearchSelect
          label="Reviewer"
          htmlFor="rv"
          value=""
          onChange={() => {}}
          onSearchChange={() => {}}
          options={OPTS}
          isPending={false}
          isError={false}
        />
      </div>,
    )
    const input = screen.getByLabelText('Reviewer') // no focus → list stays closed
    const esc = createEvent.keyDown(input, { key: 'Escape' })
    fireEvent(input, esc)
    expect(esc.defaultPrevented).toBe(false)
    expect(parentKeyDown).toHaveBeenCalled()
  })

  it('clearing the input clears the selection', () => {
    const { onChange } = setup({ value: 'u1' })
    const input = screen.getByLabelText('Red-teamer')
    fireEvent.change(input, { target: { value: 'a' } }) // type something first
    fireEvent.change(input, { target: { value: '' } }) // then clear → clears the filter
    expect(onChange).toHaveBeenCalledWith('')
  })

  it('editing the text after a pick clears the selection', () => {
    const { onChange } = setup({ value: 'u1' })
    fireEvent.change(screen.getByLabelText('Red-teamer'), { target: { value: 'alice@ex.co' } })
    expect(onChange).toHaveBeenCalledWith('')
  })

  describe('paging', () => {
    // jsdom has no layout: give the listbox a scroll geometry by hand.
    function scrollList(scrollTop: number) {
      const list = screen.getByRole('listbox')
      Object.defineProperty(list, 'scrollHeight', { value: 1000, configurable: true })
      Object.defineProperty(list, 'clientHeight', { value: 200, configurable: true })
      Object.defineProperty(list, 'scrollTop', {
        value: scrollTop,
        configurable: true,
        writable: true,
      })
      fireEvent.scroll(list)
      return list
    }

    it('asks for the next page when the list is scrolled near the end', () => {
      const onLoadMore = vi.fn()
      setup({ onLoadMore, hasMore: true })
      fireEvent.focus(screen.getByLabelText('Red-teamer'))
      scrollList(500)
      expect(onLoadMore).not.toHaveBeenCalled()
      scrollList(780)
      expect(onLoadMore).toHaveBeenCalledOnce()
    })

    it('asks for the next page when ↓ has no enabled option left below', () => {
      const onLoadMore = vi.fn()
      setup({
        onLoadMore,
        hasMore: true,
        options: [OPTS[0]!, { value: 'u2', label: 'bob@ex.com', disabled: true }],
      })
      const input = screen.getByLabelText('Red-teamer')
      fireEvent.focus(input)
      fireEvent.keyDown(input, { key: 'ArrowDown' }) // → alice
      expect(onLoadMore).not.toHaveBeenCalled()
      fireEvent.keyDown(input, { key: 'ArrowDown' }) // only the disabled bob below
      expect(onLoadMore).toHaveBeenCalledOnce()
    })

    it('does not ask when there is no next page or one is already loading', () => {
      const onLoadMore = vi.fn()
      const { rerender } = render(
        <SearchSelect
          label="Reviewer"
          htmlFor="rv"
          value=""
          onChange={() => {}}
          onSearchChange={() => {}}
          options={OPTS}
          isPending={false}
          isError={false}
          onLoadMore={onLoadMore}
          hasMore={false}
        />,
      )
      fireEvent.focus(screen.getByLabelText('Reviewer'))
      scrollList(800)

      rerender(
        <SearchSelect
          label="Reviewer"
          htmlFor="rv"
          value=""
          onChange={() => {}}
          onSearchChange={() => {}}
          options={OPTS}
          isPending={false}
          isError={false}
          onLoadMore={onLoadMore}
          hasMore
          isLoadingMore
        />,
      )
      scrollList(800)
      expect(onLoadMore).not.toHaveBeenCalled()
      expect(within(screen.getByRole('listbox')).getByText('Loading more…')).toBeInTheDocument()
      expect(screen.getByRole('status')).toHaveTextContent('Loading more…')
    })

    it('scrolls back to the top when the term changes', () => {
      setup({ onLoadMore: vi.fn(), hasMore: true })
      const input = screen.getByLabelText('Red-teamer')
      fireEvent.focus(input)
      const list = scrollList(300)
      fireEvent.change(input, { target: { value: 'b' } })
      expect(list.scrollTop).toBe(0)
    })
  })

  it('after a pick, typing replaces the label and Backspace empties the box', async () => {
    const onSearchChange = vi.fn()
    function Controlled() {
      const [value, setValue] = useState('')
      return (
        <SearchSelect
          label="Red-teamer"
          htmlFor="rt"
          value={value}
          onChange={setValue}
          onSearchChange={onSearchChange}
          options={OPTS}
          isPending={false}
          isError={false}
        />
      )
    }
    render(<Controlled />)
    const u = userEvent.setup()
    const input = screen.getByLabelText('Red-teamer')

    await u.click(input)
    await u.click(screen.getByRole('option', { name: 'bob@ex.com' }))
    expect(input).toHaveValue('bob@ex.com')
    await u.type(input, 'a')
    expect(input).toHaveValue('a')
    await waitFor(() => expect(onSearchChange).toHaveBeenLastCalledWith('a'), { timeout: 800 })

    await u.click(screen.getByRole('option', { name: 'alice@ex.com' }))
    await u.type(input, '{Backspace}')
    expect(input).toHaveValue('')
  })

  it('shows loading / error / empty states while open', () => {
    const { rerender } = render(
      <SearchSelect
        label="Reviewer"
        htmlFor="rv"
        value=""
        onChange={() => {}}
        onSearchChange={() => {}}
        options={[]}
        isPending
        isError={false}
      />,
    )
    fireEvent.focus(screen.getByLabelText('Reviewer'))
    // Assert the visible status row inside the listbox; the same text also lives in the sr-only
    // live region (a sibling of the listbox), so scope to the listbox to keep the match unambiguous.
    expect(within(screen.getByRole('listbox')).getByText(/searching/i)).toBeInTheDocument()

    rerender(
      <SearchSelect
        label="Reviewer"
        htmlFor="rv"
        value=""
        onChange={() => {}}
        onSearchChange={() => {}}
        options={[]}
        isPending={false}
        isError
      />,
    )
    expect(within(screen.getByRole('listbox')).getByText(/could not load/i)).toBeInTheDocument()

    rerender(
      <SearchSelect
        label="Reviewer"
        htmlFor="rv"
        value=""
        onChange={() => {}}
        onSearchChange={() => {}}
        options={[]}
        isPending={false}
        isError={false}
      />,
    )
    expect(within(screen.getByRole('listbox')).getByText(/no matches/i)).toBeInTheDocument()
  })
})
