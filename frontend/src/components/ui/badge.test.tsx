import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Badge } from './badge'

describe('Badge variants', () => {
  it('does not transform the case of a tag', () => {
    // A tag's key and value are operator-authored text, stored and sent as typed, so uppercasing
    // them misreports what was sent, and the class list is the only observable in jsdom (no stylesheet
    // is loaded, and CSS casing never reaches the accessible name).
    render(<Badge variant="tag">persona: Dr. Smith</Badge>)

    // The family and the tracking are pinned alongside the case: free text in the machine-token
    // face misreads the same way a case transform does.
    const chip = screen.getByText('persona: Dr. Smith')
    expect(chip).toHaveClass('bg-muted') // the variant exists and is the muted chip face
    expect(chip).not.toHaveClass('uppercase')
    expect(chip).not.toHaveClass('font-mono')
    expect(chip).not.toHaveClass('tracking-wide')
  })

  it.each(['ok', 'warn', 'new', 'err', 'neutral'] as const)(
    'renders the %s status pill as typed, in the body face',
    (variant) => {
      render(<Badge variant={variant}>pending approval</Badge>)

      const pill = screen.getByText('pending approval')
      expect(pill).not.toHaveClass('uppercase')
      expect(pill).not.toHaveClass('font-mono')
    },
  )

  it.each([
    ['ok', 'bg-ok-surface', 'text-ok'],
    ['warn', 'bg-warn-surface', 'text-warn'],
    ['new', 'bg-new-surface', 'text-new'],
    ['err', 'bg-err-surface', 'text-err'],
  ] as const)('pairs the %s tone with its own surface', (variant, surface, ink) => {
    // A tint fallback like `bg-ok/15` would look close enough to pass a glance, so pin both halves.
    render(<Badge variant={variant}>status</Badge>)

    expect(screen.getByText('status')).toHaveClass(surface, ink)
  })
})
