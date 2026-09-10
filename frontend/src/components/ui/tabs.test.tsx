import type { FormEvent } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Tabs } from './tabs'
import { tabPanelProps } from './tab-panel'

// Three, not two: with a pair, ArrowLeft and ArrowRight land on the same tab and a swapped
// direction cannot be caught.
const TABS = [
  { value: 'queue', label: 'Queue' },
  { value: 'all', label: 'All' },
  { value: 'mine', label: 'Mine' },
]

describe('Tabs', () => {
  it('renders all tab labels', () => {
    render(<Tabs tabs={TABS} value="queue" onChange={() => {}} />)
    expect(screen.getByRole('tab', { name: 'Queue' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'All' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Mine' })).toBeInTheDocument()
  })

  it('active tab has aria-selected=true', () => {
    render(<Tabs tabs={TABS} value="queue" onChange={() => {}} />)
    expect(screen.getByRole('tab', { name: 'Queue' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tab', { name: 'All' })).toHaveAttribute('aria-selected', 'false')
  })

  it('clicking inactive tab calls onChange with its value', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<Tabs tabs={TABS} value="queue" onChange={onChange} />)
    await user.click(screen.getByRole('tab', { name: 'All' }))
    expect(onChange).toHaveBeenCalledWith('all')
  })

  it('tabs are type="button" so they never submit an enclosing form', async () => {
    const user = userEvent.setup()
    const onSubmit = vi.fn((e: FormEvent) => e.preventDefault())
    render(
      <form onSubmit={onSubmit}>
        <Tabs tabs={TABS} value="queue" onChange={() => {}} />
      </form>,
    )

    await user.click(screen.getByRole('tab', { name: 'All' }))

    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('is one stop in the tab sequence — only the selected tab is reachable by Tab', () => {
    render(<Tabs tabs={TABS} value="queue" onChange={() => {}} />)
    expect(screen.getByRole('tab', { name: 'Queue' })).toHaveAttribute('tabindex', '0')
    expect(screen.getByRole('tab', { name: 'All' })).toHaveAttribute('tabindex', '-1')
    expect(screen.getByRole('tab', { name: 'Mine' })).toHaveAttribute('tabindex', '-1')
  })

  it('points the selected tab at the panel it controls, and inactive tabs at nothing', () => {
    render(<Tabs tabs={TABS} value="queue" onChange={() => {}} />)
    expect(screen.getByRole('tab', { name: 'Queue' })).toHaveAttribute(
      'aria-controls',
      tabPanelProps('queue').id,
    )
    // The other panel is not in the DOM, so pointing at its id would dangle.
    expect(screen.getByRole('tab', { name: 'All' })).not.toHaveAttribute('aria-controls')
  })

  it('the panel props name the tab that labels them', () => {
    render(
      <>
        <Tabs tabs={TABS} value="queue" onChange={() => {}} />
        <div {...tabPanelProps('queue')}>panel body</div>
      </>,
    )
    const panel = screen.getByRole('tabpanel')
    expect(panel).toHaveAccessibleName('Queue')
    expect(panel).toHaveAttribute('tabindex', '0')
  })

  it('arrows move focus along the tablist and wrap at both ends', async () => {
    const user = userEvent.setup()
    render(<Tabs tabs={TABS} value="queue" onChange={() => {}} />)
    const [queue, all, mine] = [
      screen.getByRole('tab', { name: 'Queue' }),
      screen.getByRole('tab', { name: 'All' }),
      screen.getByRole('tab', { name: 'Mine' }),
    ]
    queue.focus()
    await user.keyboard('{ArrowRight}')
    expect(all).toHaveFocus()
    await user.keyboard('{ArrowRight}')
    expect(mine).toHaveFocus()
    await user.keyboard('{ArrowRight}')
    expect(queue).toHaveFocus() // wraps forwards off the last
    await user.keyboard('{ArrowLeft}')
    expect(mine).toHaveFocus() // and backwards off the first
    await user.keyboard('{Home}')
    expect(queue).toHaveFocus()
    await user.keyboard('{End}')
    expect(mine).toHaveFocus()
  })

  it('arrows do not select — activation stays on Enter/Space, so Back is not buried', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<Tabs tabs={TABS} value="queue" onChange={onChange} />)
    screen.getByRole('tab', { name: 'Queue' }).focus()
    await user.keyboard('{ArrowRight}')
    expect(onChange).not.toHaveBeenCalled()
    await user.keyboard('{Enter}')
    expect(onChange).toHaveBeenCalledWith('all')
  })

  it('clicking already-active tab still calls onChange', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<Tabs tabs={TABS} value="queue" onChange={onChange} />)
    await user.click(screen.getByRole('tab', { name: 'Queue' }))
    expect(onChange).toHaveBeenCalledWith('queue')
  })
})
