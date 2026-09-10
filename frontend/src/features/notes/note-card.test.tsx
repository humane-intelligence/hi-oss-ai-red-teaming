import { afterEach, beforeAll, describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { NoteCard } from './note-card'
import type { NoteResponse } from '@/lib/api/types'

// jsdom lays nothing out, so both heights are 0 and the clamp can never be observed. These two
// stubs are what let the *measurement* be tested at all: `ResizeObserver` because jsdom has none,
// and the height pair because a clipped note is exactly `scrollHeight > clientHeight`.
beforeAll(() => {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
})

function stubHeights(scrollHeight: number, clientHeight: number) {
  const original = {
    scroll: Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollHeight'),
    client: Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientHeight'),
  }
  Object.defineProperty(HTMLElement.prototype, 'scrollHeight', {
    configurable: true,
    get: () => scrollHeight,
  })
  Object.defineProperty(HTMLElement.prototype, 'clientHeight', {
    configurable: true,
    get: () => clientHeight,
  })
  return () => {
    if (original.scroll)
      Object.defineProperty(HTMLElement.prototype, 'scrollHeight', original.scroll)
    if (original.client)
      Object.defineProperty(HTMLElement.prototype, 'clientHeight', original.client)
  }
}

let restore: (() => void) | null = null
afterEach(() => {
  restore?.()
  restore = null
})

const note: NoteResponse = {
  id: 'note-0001',
  conversation_id: 'conv-0001',
  message_ids: ['msg-0001'],
  text: 'Four short paragraphs that each wrap at a narrow width.',
  created_by_id: 'user-0001',
  evaluation_id: 'eval-0001',
  evaluation_group_id: 'group-0001',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

describe('NoteCard — the expand control', () => {
  it('offers Show more when the body is clipped, however short its text is', async () => {
    // The case a length/newline heuristic missed: few explicit lines, each wrapping past the clamp.
    restore = stubHeights(160, 120)
    const user = userEvent.setup()
    render(<NoteCard note={note} />)

    const button = screen.getByRole('button', { name: /show more/i })
    expect(button).toHaveAttribute('aria-expanded', 'false')
    expect(button).toHaveAttribute('aria-controls', `note-body-${note.id}`)

    await user.click(button)
    expect(screen.getByRole('button', { name: /show less/i })).toHaveAttribute(
      'aria-expanded',
      'true',
    )
  })

  it('offers no control when nothing is clipped', () => {
    restore = stubHeights(120, 120)
    render(<NoteCard note={note} />)

    expect(screen.queryByRole('button', { name: /show (more|less)/i })).toBeNull()
  })
})
