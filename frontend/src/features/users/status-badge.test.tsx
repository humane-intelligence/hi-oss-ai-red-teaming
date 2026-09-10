import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { UserStatusBadge } from './status-badge'
import type { UserStatus } from '@/lib/api/types'

const STATUSES: UserStatus[] = ['active', 'pending', 'invited', 'inactive']

describe('UserStatusBadge', () => {
  it('spells the status out when not compact', () => {
    render(<UserStatusBadge status="invited" />)

    expect(screen.getByText('invited')).toBeInTheDocument()
  })

  // Both are `warn`, and the Invitation column that would disambiguate waits for `xl`.
  it('gives every status its own compact code', () => {
    const codes = STATUSES.map((status) => {
      const { unmount } = render(<UserStatusBadge status={status} compact />)
      const code = screen.getByText(/^(ACT|PEN|INV|OFF)$/).textContent
      unmount()
      return code
    })

    expect(new Set(codes).size).toBe(STATUSES.length)
    expect(codes).toEqual(['ACT', 'PEN', 'INV', 'OFF'])
  })

  it('keeps the full status as the accessible name in compact form', () => {
    render(<UserStatusBadge status="pending" compact />)

    // The code is decorative; the badge carries the real word for assistive tech.
    expect(screen.getByLabelText('pending')).toBeInTheDocument()
    expect(screen.getByText('PEN')).toHaveAttribute('aria-hidden')
  })

  it('renders the code and the word together, one hidden per breakpoint', () => {
    render(<UserStatusBadge status="inactive" compact />)

    expect(screen.getByText('OFF').className).toMatch(/sm:hidden/)
    expect(screen.getByText('inactive').className).toMatch(/hidden sm:inline/)
  })
})
