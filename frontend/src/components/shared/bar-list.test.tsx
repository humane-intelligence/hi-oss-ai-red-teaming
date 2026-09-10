import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { BarList } from './bar-list'

const rows = [
  { key: 'a1', label: 'gpt-4o-mini', value: 1200 },
  { key: 'a2', label: 'claude-haiku', value: 0 },
]

describe('BarList', () => {
  it('renders one row per category with its count', () => {
    render(<BarList title="Tokens by model" rows={rows} emptyLabel="Nothing here." />)

    expect(screen.getByText('Tokens by model')).toBeInTheDocument()
    expect(screen.getByText('gpt-4o-mini')).toBeInTheDocument()
    expect(screen.getByText('1,200')).toBeInTheDocument()
    // Zero-inclusive rosters need an unused model to read as unused, not to go missing.
    expect(screen.getByText('claude-haiku')).toBeInTheDocument()
    expect(screen.getByText('0')).toBeInTheDocument()
  })

  it('shows the caller’s empty label rather than a generic one', () => {
    // "attribution withheld" must not read as "nothing assigned".
    render(<BarList title="Tokens by model" rows={[]} emptyLabel="Attribution withheld." />)

    expect(screen.getByText('Attribution withheld.')).toBeInTheDocument()
    expect(screen.queryByText('gpt-4o-mini')).toBeNull()
  })

  it('keys rows by their key, not their label', () => {
    // Removing a row and checking the survivor does *not* discriminate: with duplicate keys React
    // still updates the surviving node's props, so the output is right either way. The warning is
    // the only observable difference, and `src/test/setup.ts` does not fail on console.error.
    const warn = vi.spyOn(console, 'error').mockImplementation(() => {})
    render(
      <BarList
        title="Tokens by model"
        rows={[
          { key: 'a1', label: 'Masked model', value: 30 },
          { key: 'a2', label: 'Masked model', value: 10 },
        ]}
        emptyLabel="Nothing here."
      />,
    )

    expect(screen.getAllByText('Masked model')).toHaveLength(2)
    expect(warn.mock.calls.flat().join(' ')).not.toMatch(/same key/i)
    warn.mockRestore()
  })

  it('renders the hint only when one is given', () => {
    const { rerender } = render(
      <BarList title="Tokens by model" rows={rows} emptyLabel="Nothing here." />,
    )
    expect(screen.queryByRole('button')).toBeNull()

    rerender(
      <BarList
        title="Tokens by model"
        hint="Rows can sum to less than the total above."
        rows={rows}
        emptyLabel="Nothing here."
      />,
    )
    expect(
      screen.getByRole('button', { name: 'Rows can sum to less than the total above.' }),
    ).toBeInTheDocument()
  })
})
