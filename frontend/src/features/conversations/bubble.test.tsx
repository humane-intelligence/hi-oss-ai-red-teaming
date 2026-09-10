import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Bubble } from './bubble'

const NONE = new Set<string>()

describe('Bubble — per-message tags', () => {
  it('renders tag chips when the message carries tags', () => {
    render(<Bubble role="user" content="hi" tags={{ turn: '1', env: 'prod' }} unsentTags={NONE} />)
    const chips = screen.getByTestId('message-tags')
    expect(chips).toHaveTextContent('turn: 1')
    expect(chips).toHaveTextContent('env: prod')
  })

  it('renders a bare key when the value is empty', () => {
    render(<Bubble role="user" content="hi" tags={{ flag: '' }} unsentTags={NONE} />)
    expect(screen.getByTestId('message-tags')).toHaveTextContent('flag')
    expect(screen.getByTestId('message-tags')).not.toHaveTextContent('flag:')
  })

  it('renders no tag row when there are none', () => {
    render(<Bubble role="user" content="hi" />)
    expect(screen.queryByTestId('message-tags')).toBeNull()
  })

  it('marks a tag the policy keeps out of the prompt, and only that one', () => {
    // Message tags are immutable, so an unmarked chip is the transcript's least correctable claim
    // about what the model saw — the marker is the whole point of threading the set in.
    render(
      <Bubble
        role="assistant"
        content="hi"
        tags={{ env: 'prod', legacy: 'x' }}
        unsentTags={new Set(['legacy'])}
      />,
    )

    const chips = screen.getByTestId('message-tags')
    expect(chips.querySelectorAll('li')).toHaveLength(2)
    // One marker, on the dropped key: the row carrying `legacy` is the one that says so.
    const marked = [...chips.querySelectorAll('li')].filter((li) =>
      li.textContent?.includes('(not sent to the model)'),
    )
    expect(marked).toHaveLength(1)
    expect(marked[0]).toHaveTextContent('legacy: x')
  })

  it('puts the same label in the chip and its tooltip, marker included', () => {
    render(<Bubble role="user" content="hi" tags={{ blank: '' }} unsentTags={new Set(['blank'])} />)

    // A valueless tag reads as the bare key in both places — no dangling `blank: ` in the title.
    expect(screen.getByTitle('blank — not sent to the model')).toBeInTheDocument()
  })
})

describe('Bubble — recorded tag context', () => {
  it('shows the tag context an assistant reply recorded', () => {
    render(<Bubble role="assistant" content="hi" tagContext={{ env: 'prod' }} />)

    expect(screen.getByTestId('tag-context')).toHaveTextContent('env: prod')
  })

  it('shows none on a user message', () => {
    render(<Bubble role="user" content="hi" tagContext={{ env: 'prod' }} />)

    expect(screen.queryByTestId('tag-context')).not.toBeInTheDocument()
  })

  it('passes the partial flag through to the tag context block', () => {
    render(<Bubble role="assistant" content="hi" tagContext={{ env: 'prod' }} tagContextPartial />)

    expect(screen.getByTestId('tag-context')).toHaveTextContent('sent with the continuation only')
  })
})
