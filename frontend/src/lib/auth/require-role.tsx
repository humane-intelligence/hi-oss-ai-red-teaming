import type { ReactNode } from 'react'
import { Outlet } from 'react-router-dom'
import { usePermissions } from './use-permissions'
import { NotAuthorized } from './not-authorized'

export function RequireRole({ anyOf, children }: { anyOf: string[]; children?: ReactNode }) {
  const { hasRole } = usePermissions()
  if (!anyOf.some((r) => hasRole(r))) return <NotAuthorized />
  return <>{children ?? <Outlet />}</>
}
