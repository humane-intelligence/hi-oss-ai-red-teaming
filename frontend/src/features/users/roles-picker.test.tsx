import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { RolesPicker } from './roles-picker'
import type { RoleResponse } from '@/lib/api/types'

const SYSTEM: RoleResponse = {
  id: 's',
  name: 'red_teamer',
  display_name: 'Red Teamer',
  permissions: [],
  is_system: true,
  is_active: true,
  is_default: false,
  is_participant_default: false,
  is_object_assignable: true,
}
const CUSTOM: RoleResponse = {
  ...SYSTEM,
  id: 'c',
  name: 'lead',
  display_name: 'Lead Reviewer',
  is_system: false,
  is_object_assignable: false,
}

function renderPicker(roles: RoleResponse[], isPending = false, isError = false) {
  render(
    <RolesPicker
      roles={roles}
      isPending={isPending}
      isError={isError}
      selected={[]}
      onChange={() => {}}
    />,
  )
}

describe('RolesPicker', () => {
  it('offers exactly the roles it is given, custom ones included', () => {
    renderPicker([SYSTEM, CUSTOM])

    expect(screen.getByText('Red Teamer')).toBeInTheDocument()
    expect(screen.getByText('Lead Reviewer')).toBeInTheDocument()
  })

  it('says it is loading while the roles are pending', () => {
    renderPicker([], true)

    expect(screen.getByText(/loading roles/i)).toBeInTheDocument()
  })

  it('renders an empty-state when there are no roles to offer', () => {
    renderPicker([])

    expect(screen.getByText(/no roles available to pick/i)).toBeInTheDocument()
  })

  it('says the roles could not be loaded instead of offering none', () => {
    renderPicker([], false, true)

    expect(screen.getByText(/couldn’t load the roles/i)).toBeInTheDocument()
    expect(screen.queryByText(/no roles available/i)).toBeNull()
  })
})
