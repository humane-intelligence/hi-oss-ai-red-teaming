import type { ReactNode } from 'react'
import { Outlet } from 'react-router-dom'
import { usePermissions } from './use-permissions'
import { NotAuthorized } from './not-authorized'

export function RequirePermission({ anyOf, children }: { anyOf: string[]; children?: ReactNode }) {
  const { hasAny } = usePermissions()
  if (!hasAny(anyOf)) return <NotAuthorized />
  return <>{children ?? <Outlet />}</>
}
