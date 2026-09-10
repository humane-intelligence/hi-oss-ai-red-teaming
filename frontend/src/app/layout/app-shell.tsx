import { Suspense, useEffect, useRef, useState } from 'react'
import { NavLink, Outlet } from 'react-router-dom'
import { Loader2, LogOut, Menu, Moon, Sun } from 'lucide-react'
import { useAuth } from '@/lib/auth/auth-context'
import { usePermissions } from '@/lib/auth/use-permissions'
import { useExports } from '@/features/exports/exports-context'
import { NotificationBell } from '@/features/notifications/notification-bell'
import { useTheme } from '@/lib/theme'
import { Button } from '@/components/ui/button'
import { Drawer } from '@/components/ui/drawer'
import { DetailSkeleton } from '@/components/shared/detail-skeleton'
import { SidebarNav } from './sidebar-nav'
import { NavTrailSlotContext } from './nav-trail-slot'

// Tailwind's default `md`. The drawer's trigger is `md:hidden`, so this query and that class have
// to agree; if a `--breakpoint-md` override ever lands in index.css, this is the other half.
const MD_BREAKPOINT_PX = 768

export function AppShell() {
  const { user, logout } = useAuth()
  const { theme, toggle } = useTheme()
  const { has } = usePermissions()
  const { activeCount } = useExports()
  const [navOpen, setNavOpen] = useState(false)
  // State, not a ref: the pages that portal into this slot have to re-render once it exists, and a
  // ref mutation would not tell them.
  const [trailSlot, setTrailSlot] = useState<HTMLElement | null>(null)
  const sidebarRef = useRef<HTMLElement>(null)
  const focusSidebar = useRef(false)

  // The trigger is md:hidden, so from md up an open drawer would cover the visible sidebar with
  // nothing left to close it. Only listens while open, which is the only time it matters.
  useEffect(() => {
    if (!navOpen) return
    const desktop = window.matchMedia(`(min-width: ${MD_BREAKPOINT_PX}px)`)
    const closeOnDesktop = () => {
      if (!desktop.matches) return
      focusSidebar.current = true
      setNavOpen(false)
    }
    desktop.addEventListener('change', closeOnDesktop)
    return () => desktop.removeEventListener('change', closeOnDesktop)
  }, [navOpen])

  // The trigger is display:none by then, so the dialog's focus return lands on <body>. Passive, not
  // layout: it has to run after the Drawer's close(), since focus can't leave an open modal dialog.
  useEffect(() => {
    if (navOpen || !focusSidebar.current) return
    focusSidebar.current = false
    sidebarRef.current?.querySelector<HTMLAnchorElement>('nav a')?.focus()
  }, [navOpen])

  return (
    <div className="flex min-h-svh">
      {/* Persistent sidebar from md up; below md it collapses into the drawer opened from the header. */}
      <aside
        ref={sidebarRef}
        className="bg-sidebar text-sidebar-foreground hidden w-60 shrink-0 flex-col border-r p-3 md:flex"
      >
        <SidebarNav />
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        {/* Below md the hamburger is the only way into the nav, so the header stays reachable
            mid-list; from md up the persistent sidebar covers that and it can scroll away. */}
        <header className="bg-card sticky top-0 z-30 flex h-14 items-center gap-3 border-b px-4 md:relative md:z-auto md:px-6">
          {activeCount > 0 && (
            <div
              className="bg-primary/20 absolute inset-x-0 top-0 h-0.5 overflow-hidden"
              aria-hidden
            >
              <div className="bg-primary h-full w-full animate-pulse" />
            </div>
          )}
          <Button
            variant="ghost"
            size="icon"
            className="md:hidden"
            aria-label="Open navigation"
            // Not aria-expanded: while the drawer is open the trigger sits behind a modal and can't
            // be reached, so an "expanded" state is never observable. This says what the button does.
            aria-haspopup="dialog"
            onClick={() => setNavOpen(true)}
          >
            <Menu className="size-5" />
          </Button>
          <Drawer open={navOpen} onOpenChange={setNavOpen} label="Navigation">
            <SidebarNav onNavigate={() => setNavOpen(false)} />
          </Drawer>
          {/* Compact logo keeps branding on mobile while the drawer is closed. */}
          <NavLink to="/" className="font-display text-sm font-bold tracking-[0.16em] md:hidden">
            RED<span className="text-primary">·</span>TEAM
          </NavLink>

          {/* Doubles as the spacer that pushes the controls right, so it is present at every width;
              only its content is desktop-only (see the trail's own wrapper). `min-w-0` + the trail's
              `overflow-hidden` keep a long organization or group name from wrapping this
              fixed-height header, which would push the controls off-screen. */}
          <div ref={setTrailSlot} className="min-w-0 flex-1" />
          {activeCount > 0 && (
            <span
              className="text-muted-foreground flex items-center gap-1.5 font-mono text-xs"
              role="status"
              aria-live="polite"
            >
              <Loader2 className="size-3.5 animate-spin" />
              {/* Narrow headers keep the spinner and drop only the wording: this is the sole
                  surface for an in-flight export, and the live region must stay announceable. */}
              <span className="sr-only sm:not-sr-only">Exporting {activeCount}…</span>
            </span>
          )}
          {/* Revealed at the same breakpoint as the sidebar, and capped: a long address would
              otherwise wrap inside the fixed-height header and push the controls off-screen. */}
          {user && (
            <NavLink
              to="/account"
              // The accessible name must contain the visible text (WCAG 2.5.3).
              aria-label={`Account settings (${user.email})`}
              className="text-muted-foreground hover:text-foreground hidden max-w-[14rem] min-w-0 truncate font-mono text-xs hover:underline md:inline"
            >
              {user.email}
            </NavLink>
          )}
          {has('notifications:read') && <NotificationBell />}
          <Button variant="ghost" size="icon" onClick={toggle} aria-label="Toggle theme">
            {theme === 'dark' ? <Sun className="size-4" /> : <Moon className="size-4" />}
          </Button>
          <Button variant="ghost" size="sm" onClick={logout} aria-label="Sign out">
            <LogOut className="size-4" />
            <span className="hidden sm:inline">Sign out</span>
          </Button>
        </header>
        <main className="flex-1 overflow-auto p-4 md:p-8">
          <Suspense fallback={<DetailSkeleton />}>
            <NavTrailSlotContext.Provider value={trailSlot}>
              <Outlet />
            </NavTrailSlotContext.Provider>
          </Suspense>
        </main>
      </div>
    </div>
  )
}
