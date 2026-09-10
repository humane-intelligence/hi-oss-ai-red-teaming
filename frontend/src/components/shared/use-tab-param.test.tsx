import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { useTabParam } from './use-tab-param'

const TABS = ['overview', 'metrics', 'exports'] as const

function Probe() {
  const [active, setActive] = useTabParam(TABS, 'overview')
  const location = useLocation()
  const navigate = useNavigate()
  return (
    <div>
      <span data-testid="active">{active}</span>
      <span data-testid="search">{location.search}</span>
      <span data-testid="path">{location.pathname}</span>
      <button onClick={() => setActive('metrics')}>go metrics</button>
      <button onClick={() => setActive('overview')}>go overview</button>
      <button onClick={() => navigate(-1)}>back</button>
    </div>
  )
}

function renderAt(entry: string, previous?: string) {
  const entries = previous ? [previous, entry] : [entry]
  render(
    <MemoryRouter initialEntries={entries} initialIndex={entries.length - 1}>
      <Routes>
        <Route path="/g" element={<Probe />} />
        <Route path="/elsewhere" element={<span data-testid="path">/elsewhere</span>} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('useTabParam', () => {
  it('falls back to the first tab when the param is absent', () => {
    renderAt('/g')
    expect(screen.getByTestId('active')).toHaveTextContent('overview')
  })

  it('reads a recognised param', () => {
    renderAt('/g?tab=exports')
    expect(screen.getByTestId('active')).toHaveTextContent('exports')
  })

  it('falls back on an unrecognised param and drops it from the URL', () => {
    renderAt('/g?tab=nonsense')
    expect(screen.getByTestId('active')).toHaveTextContent('overview')
    // The bad value must not survive in the URL, or Back returns the user to it.
    expect(screen.getByTestId('search')).not.toHaveTextContent('nonsense')
  })

  it('writes the param when the tab changes', async () => {
    const user = userEvent.setup()
    renderAt('/g')
    await user.click(screen.getByRole('button', { name: 'go metrics' }))
    expect(screen.getByTestId('active')).toHaveTextContent('metrics')
    expect(screen.getByTestId('search')).toHaveTextContent('tab=metrics')
  })

  // The two behaviours the hook exists for, and the two the comment above it promises.
  it('drops the param again when the default tab is selected, leaving a clean URL', async () => {
    const user = userEvent.setup()
    renderAt('/g?tab=metrics')
    await user.click(screen.getByRole('button', { name: 'go overview' }))
    expect(screen.getByTestId('active')).toHaveTextContent('overview')
    expect(screen.getByTestId('search').textContent).toBe('')
  })

  it('pushes a tab switch, so Back returns to the previous tab', async () => {
    const user = userEvent.setup()
    renderAt('/g')
    await user.click(screen.getByRole('button', { name: 'go metrics' }))
    expect(screen.getByTestId('search')).toHaveTextContent('tab=metrics')
    await user.click(screen.getByRole('button', { name: 'back' }))
    expect(screen.getByTestId('search').textContent).toBe('')
    expect(screen.getByTestId('active')).toHaveTextContent('overview')
  })

  it('replaces when dropping an unrecognised param, so Back skips the broken URL', async () => {
    const user = userEvent.setup()
    renderAt('/g?tab=nonsense', '/elsewhere')
    expect(screen.getByTestId('search').textContent).toBe('')
    await user.click(screen.getByRole('button', { name: 'back' }))
    expect(screen.getByTestId('path')).toHaveTextContent('/elsewhere')
  })

  it('keeps unrelated query params when switching tabs', async () => {
    const user = userEvent.setup()
    renderAt('/g?highlight=abc')
    await user.click(screen.getByRole('button', { name: 'go metrics' }))
    expect(screen.getByTestId('search')).toHaveTextContent('highlight=abc')
    expect(screen.getByTestId('search')).toHaveTextContent('tab=metrics')
  })
})
