import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { RequirePermission } from './require-permission'
import { authWrapper } from './auth.testutils'

describe('RequirePermission', () => {
  it('renders children when the user has one of the permissions', () => {
    const Wrapper = authWrapper(['evaluations:approve'])
    render(
      <Wrapper>
        <RequirePermission anyOf={['evaluations:approve']}>
          <div>SECRET</div>
        </RequirePermission>
      </Wrapper>,
    )
    expect(screen.getByText('SECRET')).toBeInTheDocument()
  })

  it('renders the not-authorized fallback when the user lacks all permissions', () => {
    const Wrapper = authWrapper(['flags:create'])
    render(
      <Wrapper>
        <RequirePermission anyOf={['evaluations:approve']}>
          <div>SECRET</div>
        </RequirePermission>
      </Wrapper>,
    )
    expect(screen.queryByText('SECRET')).toBeNull()
    expect(screen.getByText(/don't have access/i)).toBeInTheDocument()
  })
})
