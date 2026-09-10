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
