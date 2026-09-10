import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MultiSearchSelect } from './multi-search-select'

const OPTS = [
  { value: 'u1', label: 'a@x' },
  { value: 'u2', label: 'b@x' },
]

function setup(overrides: Partial<Parameters<typeof MultiSearchSelect>[0]> = {}) {
  const onChange = vi.fn()
  const onSearchChange = vi.fn()
  const props = {
    label: 'Reviewers',
    htmlFor: 'rv',
    value: [],
    onChange,
    onSearchChange,
    options: OPTS,
    isPending: false,
    isError: false,
    ...overrides,
  }
  const { rerender } = render(<MultiSearchSelect {...props} />)
  return {
    onChange,
    onSearchChange,
    rerenderWith: (next: Partial<Parameters<typeof MultiSearchSelect>[0]>) =>
      rerender(<MultiSearchSelect {...props} {...next} />),
  }
}

const announcement = () => screen.getByTestId('multi-select-announcement')
const optionStatus = () => screen.getByTestId('multi-select-status')

describe('MultiSearchSelect (multi-select typeahead)', () => {
  it('toggles an option on and keeps the list open', async () => {
    const { onChange } = setup()
    const u = userEvent.setup()
    await u.click(screen.getByLabelText('Reviewers'))
    await u.click(screen.getByRole('option', { name: /a@x/ }))
    expect(onChange).toHaveBeenCalledWith(['u1'])
    expect(screen.getByRole('listbox')).toBeInTheDocument() // stays open (unlike single-select)
  })

  it("points the combobox at the caller's prose", () => {
    // The create-a-new-value affordance is not discoverable from the combobox role, so a caller
    // enabling `allowCreate` has to be able to describe it.
    setup({ allowCreate: true, describedBy: 'the-hint' })
    expect(screen.getByLabelText('Reviewers')).toHaveAttribute('aria-describedby', 'the-hint')
  })

  it('announces a chip going on and coming off', async () => {
    const { onChange } = setup()
    const u = userEvent.setup()
    await u.click(screen.getByLabelText('Reviewers'))
    await u.click(screen.getByRole('option', { name: /a@x/ }))
    // Without this the only feedback for a created value is the input clearing, which is silent.
    expect(announcement()).toHaveTextContent('Added a@x')
    expect(onChange).toHaveBeenCalledWith(['u1'])
  })

  it('announces a removal, not another addition', async () => {
    setup({ value: ['u1'] })
    const u = userEvent.setup()
    await u.click(screen.getByLabelText('Reviewers'))
    await u.click(screen.getByRole('option', { name: /a@x/ }))
    expect(announcement()).toHaveTextContent('Removed a@x')
  })

  it('announces the option status after a pick, which one region could not do', async () => {
    // The chip announcement used to mask `statusText` until the next keystroke — and the create flow
    // refetches the options right after a pick, so a query that died there announced nothing at all.
    // Separate regions rather than one joined string, which would re-read the chip on every flip.
    const { rerenderWith } = setup()
    const u = userEvent.setup()
    await u.click(screen.getByLabelText('Reviewers'))
    await u.click(screen.getByRole('option', { name: /a@x/ }))
    expect(announcement()).toHaveTextContent('Added a@x')

    rerenderWith({ options: [], isError: true })
    expect(optionStatus()).toHaveTextContent('Could not load results.')
    expect(announcement()).toHaveTextContent('Added a@x')
  })

  it('says a re-typed value is already added, not that there is nothing', async () => {
    // The create row is suppressed because the value is already picked, so the generic empty label
    // would tell the operator the opposite of the truth.
    setup({
      allowCreate: true,
      value: ['audited'],
      options: [],
      emptyLabel: 'No labels in use yet.',
    })
    const u = userEvent.setup()
    await u.type(screen.getByLabelText('Reviewers'), 'audited')
    const list = screen.getByRole('listbox')
    expect(list).toHaveTextContent('Already added.')
    expect(list).not.toHaveTextContent('No labels in use yet.')
  })

  it('toggles an already-selected option off', async () => {
    const { onChange } = setup({ value: ['u1'] })
    const u = userEvent.setup()
    await u.click(screen.getByLabelText('Reviewers'))
    await u.click(screen.getByRole('option', { name: /a@x/ }))
    expect(onChange).toHaveBeenCalledWith([]) // u1 removed
  })

  it('marks selected options with aria-selected on a multiselectable listbox', async () => {
    setup({ value: ['u1'] })
    const u = userEvent.setup()
    await u.click(screen.getByLabelText('Reviewers'))
    const listbox = screen.getByRole('listbox')
    expect(listbox).toHaveAttribute('aria-multiselectable', 'true')
    expect(within(listbox).getByRole('option', { name: /a@x/ })).toHaveAttribute(
      'aria-selected',
      'true',
    )
    expect(within(listbox).getByRole('option', { name: /b@x/ })).toHaveAttribute(
      'aria-selected',
      'false',
    )
  })

  it('renders selected values as removable chips and removes on click', async () => {
    const { onChange } = setup({ value: ['u1'] })
    const u = userEvent.setup()
    await u.click(screen.getByRole('button', { name: /remove a@x/i }))
    expect(onChange).toHaveBeenCalledWith([])
  })

  it('keeps the search term after a pick (persistent search box)', async () => {
    setup()
    const input = screen.getByLabelText('Reviewers')
    fireEvent.focus(input)
    fireEvent.change(input, { target: { value: 'a' } })
    await userEvent.setup().click(screen.getByRole('option', { name: /a@x/ }))
    expect((input as HTMLInputElement).value).toBe('a') // not overwritten by the pick
  })

  it('keeps a truncated label readable in full, for the caller whose values differ at the end', async () => {
    const u = userEvent.setup()
    const long = 'claude-opus-4-1-20250805-eu-central-1-provisioned'
    setup({ options: [{ value: 'm1', label: long }] })

    await u.click(screen.getByLabelText('Reviewers'))

    // The label is clipped by `truncate`, so the full value has to survive somewhere: this component
    // also backs the model picker, whose names differ only near the end.
    expect(within(screen.getByRole('option', { name: long })).getByTitle(long)).toBeInTheDocument()
  })

  it('reveals on hover what a summarised hint left out', async () => {
    // A caller whose hint is a summary (model labels) has nowhere else to put the rest: the hint is
    // what gets read out and truncated, so the full set rides the title.
    setup({
      options: [
        {
          value: 'u1',
          label: 'a@x',
          hint: 'one, two +3',
          hintTitle: 'one, two, three, four, five',
        },
      ],
    })
    const u = userEvent.setup()
    await u.click(screen.getByLabelText('Reviewers'))
    expect(screen.getByText('one, two +3')).toHaveAttribute('title', 'one, two, three, four, five')
  })

  it('titles a plain hint with itself, having nothing else to reveal', async () => {
    setup({ options: [{ value: 'u1', label: 'a@x', hint: '2 active reviews' }] })
    const u = userEvent.setup()
    await u.click(screen.getByLabelText('Reviewers'))
    expect(screen.getByText('2 active reviews')).toHaveAttribute('title', '2 active reviews')
  })

  it('renders a hint beside the option label and keeps it out of the chip', async () => {
    const u = userEvent.setup()
    const withHints = [
      { value: 'u1', label: 'ada@example.com', hint: '2 active reviews' },
      { value: 'u2', label: 'bob@example.com' },
    ]
    // Already picked, so the chip for the hinted option is on screen.
    setup({ options: withHints, value: ['u1'] })

    await u.click(screen.getByLabelText('Reviewers'))

    // In the list the hint is part of what the option announces …
    expect(
      screen.getByRole('option', { name: 'ada@example.com 2 active reviews' }),
    ).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'bob@example.com' })).toBeInTheDocument()

    // … and in the chip it is not: a chip names the picked value, it is not a status.
    const chip = screen.getByRole('button', { name: /remove ada@example\.com/i }).parentElement
    expect(chip).toHaveTextContent('ada@example.com')
    expect(chip).not.toHaveTextContent('active')
  })

  describe('allowCreate', () => {
    it('offers the typed term as a new value and picks the term itself', async () => {
      const { onChange } = setup({ allowCreate: true, options: [] })
      const input = screen.getByLabelText('Reviewers')

      fireEvent.focus(input)
      fireEvent.change(input, { target: { value: '  self-hosted  ' } })
      await userEvent.setup().click(screen.getByRole('option', { name: /create/i }))

      expect(onChange).toHaveBeenCalledWith(['self-hosted'])
    })

    it('stays off by default, so a picker over real entities cannot invent a value', () => {
      setup({ options: [] })
      const input = screen.getByLabelText('Reviewers')

      fireEvent.focus(input)
      fireEvent.change(input, { target: { value: 'not-an-id' } })

      expect(screen.queryByRole('option', { name: /create/i })).not.toBeInTheDocument()
    })

    it('does not offer to create a value the options already carry', () => {
      setup({ allowCreate: true, options: [{ value: 'self-hosted', label: 'self-hosted' }] })
      const input = screen.getByLabelText('Reviewers')

      fireEvent.focus(input)
      // Case-insensitively, because the backend dedupes labels that differ only in case.
      fireEvent.change(input, { target: { value: 'Self-Hosted' } })

      expect(screen.queryByRole('option', { name: /create/i })).not.toBeInTheDocument()
    })

    it('does not offer to create a label the options carry under an opaque value', () => {
      // The case the value check above cannot see, since the values are ids rather than wordings.
      setup({
        allowCreate: true,
        options: [{ value: '3878a313-116a-584e-8da2-a68de796d5b9', label: 'Jailbreak' }],
      })
      const input = screen.getByLabelText('Reviewers')

      fireEvent.focus(input)
      fireEvent.change(input, { target: { value: 'jailbreak' } })

      expect(screen.queryByRole('option', { name: /create/i })).not.toBeInTheDocument()
      // The option itself stays offered — this component does not filter, its caller does.
      expect(screen.getByRole('option', { name: /Jailbreak/ })).toBeInTheDocument()
    })

    it('does not offer to create a value already picked', () => {
      setup({ allowCreate: true, options: [], value: ['audited'] })
      const input = screen.getByLabelText('Reviewers')

      fireEvent.focus(input)
      fireEvent.change(input, { target: { value: 'audited' } })

      expect(screen.queryByRole('option', { name: /create/i })).not.toBeInTheDocument()
    })

    it('offers nothing for a blank term', () => {
      setup({ allowCreate: true, options: [] })
      const input = screen.getByLabelText('Reviewers')

      fireEvent.focus(input)
      fireEvent.change(input, { target: { value: '   ' } })

      expect(screen.queryByRole('option', { name: /create/i })).not.toBeInTheDocument()
    })

    it('still offers to create when the option query failed', async () => {
      const { onChange } = setup({ allowCreate: true, options: [], isError: true })
      const input = screen.getByLabelText('Reviewers')

      fireEvent.focus(input)
      fireEvent.change(input, { target: { value: 'audited' } })

      // Creating needs no options, so a dead suggestion query must not take the affordance down
      // with it — the error line joins the create row instead of replacing it.
      // Scoped to the list: the same copy also sits in the sr-only live region.
      expect(
        within(screen.getByRole('listbox')).getByText('Could not load results.'),
      ).toBeInTheDocument()
      await userEvent.setup().click(screen.getByRole('option', { name: /create/i }))
      expect(onChange).toHaveBeenCalledWith(['audited'])
    })

    it('still offers to create while the option query is in flight', () => {
      setup({ allowCreate: true, options: [], isPending: true })
      const input = screen.getByLabelText('Reviewers')

      fireEvent.focus(input)
      fireEvent.change(input, { target: { value: 'audited' } })

      expect(screen.getByRole('option', { name: /create/i })).toBeInTheDocument()
      expect(within(screen.getByRole('listbox')).getByText('Searching…')).toBeInTheDocument()
    })

    it('picks the right option when the create row shifted the indices', async () => {
      const { onChange } = setup({ allowCreate: true })
      const input = screen.getByLabelText('Reviewers')

      fireEvent.focus(input)
      // A term matching no option, so the create row takes index 0 and pushes the options down.
      fireEvent.change(input, { target: { value: 'x' } })
      await userEvent.setup().click(screen.getByRole('option', { name: 'b@x' }))

      // The real option's value, not the term, and the search box survives (only a create clears it).
      expect(onChange).toHaveBeenCalledWith(['u2'])
      expect((input as HTMLInputElement).value).toBe('x')
    })

    it('names a created value by itself in the chip, not by the create row', () => {
      // The synthetic row is kept out of the label cache, so a picked created value has no
      // `Create "…"` label to fall back on.
      setup({ allowCreate: true, options: [], value: ['audited'] })

      expect(screen.getByRole('button', { name: 'Remove audited' })).toBeInTheDocument()
    })

    it('clears the search box after a create, so several new values can be typed in a row', async () => {
      setup({ allowCreate: true, options: [] })
      const input = screen.getByLabelText('Reviewers')

      fireEvent.focus(input)
      fireEvent.change(input, { target: { value: 'audited' } })
      await userEvent.setup().click(screen.getByRole('option', { name: /create/i }))

      expect((input as HTMLInputElement).value).toBe('')
    })

    it('creates on Enter without submitting the surrounding form', async () => {
      const onSubmit = vi.fn((event: React.FormEvent) => event.preventDefault())
      const onChange = vi.fn()
      render(
        <form onSubmit={onSubmit}>
          <MultiSearchSelect
            label="Labels"
            htmlFor="lb"
            value={[]}
            onChange={onChange}
            onSearchChange={vi.fn()}
            options={[]}
            isPending={false}
            isError={false}
            allowCreate
          />
        </form>,
      )
      const input = screen.getByLabelText('Labels')

      fireEvent.focus(input)
      fireEvent.change(input, { target: { value: 'audited' } })
      fireEvent.keyDown(input, { key: 'ArrowDown' })
      fireEvent.keyDown(input, { key: 'Enter' })

      expect(onChange).toHaveBeenCalledWith(['audited'])
      expect(onSubmit).not.toHaveBeenCalled()
    })
  })
})
