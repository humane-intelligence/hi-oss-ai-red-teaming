import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Badge } from './badge'

describe('Badge variants', () => {
  it('does not transform the case of a tag', () => {
    // A tag's key and value are operator-authored text, stored and sent as typed, so uppercasing
    // them misreports what was sent, and the class list is the only observable in jsdom (no stylesheet
    // is loaded, and CSS casing never reaches the accessible name).
    render(<Badge variant="tag">persona: Dr. Smith</Badge>)

    // The family and the tracking are pinned alongside the case: a variant that dropped only
    // `uppercase` and kept `font-mono tracking-wide` still renders free text in the machine-token
    // face, which is the regression this test would otherwise wave through.
    const chip = screen.getByText('persona: Dr. Smith')
    expect(chip).toHaveClass('bg-muted') // the variant exists and is the muted chip face
    expect(chip).not.toHaveClass('uppercase')
    expect(chip).not.toHaveClass('font-mono')
    expect(chip).not.toHaveClass('tracking-wide')
  })

  it('keeps uppercase on the machine-token variant', () => {
    render(<Badge variant="neutral">pending</Badge>)

    expect(screen.getByText('pending')).toHaveClass('uppercase')
  })
})
