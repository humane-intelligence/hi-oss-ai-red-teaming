import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ChartRangePicker } from './chart-range-picker'
import { rangeOptionsFor, type ChartRange } from './chart-range-ranges'

// Long enough that every preset survives the chart's "would this window anything?" filter.
const OPTIONS = rangeOptionsFor(365)

// Stateful on purpose: with a fixed `value` the group never moves, so arrow keys would recompute
// from the same index every time and a walk across the options could not be asserted at all.
function Harness({
  start = '7',
  onChange,
}: {
  start?: ChartRange
  onChange?: (v: ChartRange) => void
}) {
  const [value, setValue] = useState<ChartRange>(start)
  return (
    <ChartRangePicker
      value={value}
      options={OPTIONS}
      onChange={(next) => {
        setValue(next)
        onChange?.(next)
      }}
    />
  )
}

const checked = () =>
  screen
    .getAllByRole('radio')
    .find((b) => b.getAttribute('aria-checked') === 'true')
    ?.textContent?.trim()

describe('ChartRangePicker', () => {
  it('offers the four ranges and marks the active one', () => {
    render(<Harness start="all" />)
    expect(screen.getByRole('radiogroup', { name: 'Time range' })).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: 'All time' })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByRole('radio', { name: '7 days' })).toHaveAttribute('aria-checked', 'false')
  })

  // Pinned because nothing in the suite noticed when this regressed to 24 px, under the floor every
  // other control on the page holds.
  it('keeps each option at the small button’s height', () => {
    render(<Harness start="7" />)
    for (const option of screen.getAllByRole('radio')) expect(option).toHaveClass('h-8')
  })

  it('reports the picked range', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<Harness start="all" onChange={onChange} />)
    await user.click(screen.getByRole('radio', { name: '30 days' }))
    expect(onChange).toHaveBeenLastCalledWith('30')
  })

  // Radio semantics, unlike the tablist next door: selection follows focus, so each key both moves
  // and picks. Asserting the resulting selection (not the spy) is what makes the walk real — a spy
  // records every call, so a wrong key that lands on an already-visited option looks identical.
  it('walks the group with arrows, Home and End, wrapping at both ends', async () => {
    const user = userEvent.setup()
    render(<Harness start="7" />)
    screen.getByRole('radio', { name: '7 days' }).focus()

    await user.keyboard('{ArrowRight}')
    expect(checked()).toBe('30 days')
    await user.keyboard('{ArrowDown}')
    expect(checked()).toBe('90 days')
    await user.keyboard('{ArrowLeft}')
    expect(checked()).toBe('30 days')
    await user.keyboard('{ArrowUp}')
    expect(checked()).toBe('7 days')
    await user.keyboard('{ArrowLeft}')
    expect(checked()).toBe('All time') // wraps backwards off the first option
    await user.keyboard('{ArrowRight}')
    expect(checked()).toBe('7 days') // and forwards off the last
    await user.keyboard('{End}')
    expect(checked()).toBe('All time')
    await user.keyboard('{Home}')
    expect(checked()).toBe('7 days')
  })

  it('keeps the tab sequence to one stop, moving it with the selection', async () => {
    const user = userEvent.setup()
    render(<Harness start="7" />)
    expect(screen.getByRole('radio', { name: '7 days' })).toHaveAttribute('tabindex', '0')
    expect(screen.getByRole('radio', { name: '30 days' })).toHaveAttribute('tabindex', '-1')

    screen.getByRole('radio', { name: '7 days' }).focus()
    await user.keyboard('{ArrowRight}')

    expect(screen.getByRole('radio', { name: '7 days' })).toHaveAttribute('tabindex', '-1')
    expect(screen.getByRole('radio', { name: '30 days' })).toHaveAttribute('tabindex', '0')
    expect(screen.getByRole('radio', { name: '30 days' })).toHaveFocus()
  })
})
