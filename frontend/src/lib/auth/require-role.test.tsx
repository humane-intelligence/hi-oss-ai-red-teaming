import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { RequireRole } from './require-role'
import { authWrapper, role } from './auth.testutils'

describe('RequireRole', () => {
  it('renders children when the user has one of the roles', () => {
    const Wrapper = authWrapper([], [role('admin')])
    render(
      <Wrapper>
        <RequireRole anyOf={['admin']}>
          <div>SECRET</div>
        </RequireRole>
      </Wrapper>,
    )
    expect(screen.getByText('SECRET')).toBeInTheDocument()
  })

  it('renders the not-authorized fallback when the user lacks all roles', () => {
    const Wrapper = authWrapper([], [role('reviewer')])
    render(
      <Wrapper>
        <RequireRole anyOf={['admin']}>
          <div>SECRET</div>
        </RequireRole>
      </Wrapper>,
    )
    expect(screen.queryByText('SECRET')).toBeNull()
    expect(screen.getByText(/don't have access/i)).toBeInTheDocument()
  })
})
