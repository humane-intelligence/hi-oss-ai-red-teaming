import { NavLink } from 'react-router-dom'
import {
  Activity,
  Building2,
  ClipboardList,
  Cpu,
  Flag,
  Gavel,
  LayoutDashboard,
  Layers,
  MessagesSquare,
  ScrollText,
  Scale,
  Settings,
  ShieldCheck,
  UserCog,
  Users,
} from 'lucide-react'
import type { ComponentType } from 'react'
import { cn } from '@/lib/utils'
import { usePermissions } from '@/lib/auth/use-permissions'

type NavItem = {
  to: string
  label: string
  icon: ComponentType<{ className?: string }>
  permsAny?: string[]
  rolesAny?: string[]
}

type NavGroup = {
  label: string
  items: NavItem[]
}

const navGroups: NavGroup[] = [
  {
    label: 'Evaluations',
    items: [
      {
        to: '/evaluation-groups',
        label: 'Evaluation Groups',
        icon: Layers,
        permsAny: ['evaluation_groups:read'],
      },
      {
        to: '/evaluations',
        label: 'Evaluations',
        icon: ClipboardList,
        permsAny: ['evaluations:read'],
      },
      {
        to: '/conversation-groups',
        label: 'Conversations',
        icon: MessagesSquare,
        permsAny: ['conversations:read'],
      },
    ],
  },
  {
    label: 'Submissions',
    items: [
      { to: '/message-flags', label: 'My flags', icon: Flag, permsAny: ['flags:read'] },
      { to: '/reviews', label: 'Reviews', icon: Gavel, permsAny: ['reviews:read'] },
    ],
  },
  {
    label: 'Inference',
    items: [{ to: '/ai-models', label: 'AI Models', icon: Cpu, permsAny: ['models:read'] }],
  },
  {
    label: 'Admin',
    items: [
      { to: '/users', label: 'Users', icon: Users, permsAny: ['users:read'] },
      { to: '/roles', label: 'Roles', icon: ShieldCheck, permsAny: ['roles:read'] },
      {
        to: '/organizations',
        label: 'Organizations',
        icon: Building2,
        permsAny: ['organizations:read'],
      },
      { to: '/audit-logs', label: 'Audit Log', icon: ScrollText, permsAny: ['audit:read'] },
      {
        to: '/licenses',
        label: 'Data Licenses',
        icon: Scale,
        permsAny: ['licenses:create', 'licenses:update', 'licenses:delete'],
      },
    ],
  },
  {
    label: 'System',
    items: [
      {
        to: '/system-preferences',
        label: 'System Preferences',
        icon: Settings,
        permsAny: ['platform_settings:read'],
      },
      { to: '/status', label: 'Backend status', icon: Activity, rolesAny: ['admin'] },
    ],
  },
  {
    label: 'Personal',
    items: [{ to: '/account', label: 'Account settings', icon: UserCog }],
  },
]

const linkClass = ({ isActive }: { isActive: boolean }) =>
  cn(
    'flex items-center gap-2 rounded-md border-l-2 px-3 py-2 text-sm transition-colors',
    isActive
      ? 'border-primary bg-sidebar-accent text-sidebar-accent-foreground'
      : 'border-transparent text-muted-foreground hover:bg-sidebar-accent/50',
  )

// Logo + permission-filtered navigation, shared verbatim by the desktop sidebar and the mobile
// drawer. `onNavigate` lets the drawer close itself when a link is followed (no-op on desktop).
export function SidebarNav({ onNavigate }: { onNavigate?: () => void }) {
  const { has, hasRole } = usePermissions()
  return (
    <>
      {/* w-fit: a full-width box would reach under the drawer's close button, so a missed tap on
          it would navigate to Overview instead of closing. */}
      <NavLink to="/" className="mb-4 block w-fit px-2 pt-1" onClick={onNavigate}>
        <div className="font-display text-lg font-bold tracking-[0.16em]">
          RED<span className="text-primary">·</span>TEAM
        </div>
        <div className="text-muted-foreground mt-0.5 font-mono text-[9px] tracking-[0.32em] uppercase">
          console
        </div>
      </NavLink>
      <nav>
        <NavLink to="/" end className={linkClass} onClick={onNavigate}>
          <LayoutDashboard className="size-4" />
          Overview
        </NavLink>
        {navGroups.map((group) => {
          const visibleItems = group.items.filter(
            (i) =>
              (!i.permsAny || i.permsAny.some((p) => has(p))) &&
              (!i.rolesAny || i.rolesAny.some((r) => hasRole(r))),
          )
          if (visibleItems.length === 0) return null
          return (
            <div key={group.label}>
              <div className="text-muted-foreground mt-5 mb-1 px-3 font-mono text-[9.5px] tracking-[0.2em] uppercase">
                {group.label}
              </div>
              {visibleItems.map(({ to, label, icon: Icon }) => (
                <NavLink key={to} to={to} className={linkClass} onClick={onNavigate}>
                  <Icon className="size-4" />
                  {label}
                </NavLink>
              ))}
            </div>
          )
        })}
      </nav>
    </>
  )
}
