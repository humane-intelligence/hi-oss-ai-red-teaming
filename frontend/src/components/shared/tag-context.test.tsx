import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { TagContext } from './tag-context'

describe('TagContext', () => {
  it('renders the recorded pairs, keys sorted', () => {
    // `jsonb` returns object keys by (length, bytes), so the wire order is not the prompt's.
    render(<TagContext tagContext={{ team: 'red', env: 'prod' }} />)

    const chips = screen.getByTestId('tag-context').textContent
    expect(chips).toContain('env: prod')
    // Both pairs asserted before the ordering: otherwise a missing `team` only shows up as a
    // confusing `0 < -1` failure rather than as the missing chip it is.
    expect(chips).toContain('team: red')
    expect(chips?.indexOf('env')).toBeLessThan(chips?.indexOf('team') ?? -1)
  })

  it('renders nothing when the reply recorded no context', () => {
    render(<TagContext tagContext={{}} />)

    expect(screen.queryByTestId('tag-context')).not.toBeInTheDocument()
  })

  it('renders nothing when the tagContext prop is omitted', () => {
    render(<TagContext />)

    expect(screen.queryByTestId('tag-context')).not.toBeInTheDocument()
  })

  it('renders nothing when tagContext is null', () => {
    render(<TagContext tagContext={null} />)

    expect(screen.queryByTestId('tag-context')).not.toBeInTheDocument()
  })

  it('says the record covers only the continuation when partial is set', () => {
    render(<TagContext tagContext={{ env: 'prod' }} partial />)

    expect(screen.getByTestId('tag-context')).toHaveTextContent('sent with the continuation only')
  })

  it('says the record covers the whole reply when partial is unset', () => {
    render(<TagContext tagContext={{ env: 'prod' }} />)

    const chips = screen.getByTestId('tag-context').textContent
    expect(chips).toContain('sent with')
    expect(chips).not.toContain('sent with the continuation')
  })

  it('renders nothing when the map is empty, even with partial set', () => {
    render(<TagContext tagContext={{}} partial />)

    expect(screen.queryByTestId('tag-context')).not.toBeInTheDocument()
  })

  it('carries no "not sent" marker, in text or in any attribute', () => {
    // Every recorded entry reached the model; the unsent guess belongs to the live-tag chips only.
    // innerHTML (not queryByText) also catches a leak through `title` or `aria-label`.
    const { container } = render(<TagContext tagContext={{ env: 'prod' }} />)

    expect(container.innerHTML).not.toMatch(/not sent/i)
  })
})
