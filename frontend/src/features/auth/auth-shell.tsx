import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { useVersion } from '@/features/status/queries'
import type { VersionResponse } from '@/lib/api/types'
import { cn } from '@/lib/utils'
import { ClientLogo } from '@/components/shared/client-logo'
import { useClientLogo } from '@/features/system-preferences/queries'

function ReadoutRow({ k, v }: { k: string; v: ReactNode }) {
  return (
    <div className="flex gap-4">
      <dt className="text-muted-foreground w-16 shrink-0">{k}</dt>
      <dd className="text-foreground/90">{v}</dd>
    </div>
  )
}

function Wordmark({ className }: { className?: string }) {
  return (
    <div className={cn('font-display tracking-[0.16em]', className)}>
      {/* `font-bold` stays on this line, not the root: the subline below pins its own family,
          size and tracking but not its weight, so a bold root would bleed into it. */}
      <div className="font-bold">
        RED<span className="text-primary">·</span>TEAM
      </div>
      <div className="text-muted-foreground mt-1 font-mono text-[10px]">operator console</div>
    </div>
  )
}

// Only the build, and only off prod (the SHA is public, but no need to advertise it there).
function VersionNote({ version }: { version: VersionResponse }) {
  return (
    <dl className="font-mono text-xs">
      <ReadoutRow k="version" v={version.version} />
    </dl>
  )
}

// One deadline over the whole hold: the settings read alone can take twice its own timeout (one
// retry), and an image can hang too, so a dead endpoint must not keep the form blank for longer.
const REVEAL_DEADLINE_MS = 5_000

export function AuthShell({
  title,
  subtitle,
  children,
}: {
  title: string
  subtitle?: string
  // Optional: a purely transitional state (e.g. the OIDC callback while it
  // completes) is a title + subtitle with nothing to act on.
  children?: ReactNode
}) {
  const { data: version, isPending: versionPending } = useVersion()
  const logo = useClientLogo()
  const hasLogo = !!(logo.light && logo.dark)
  const [logoReady, setLogoReady] = useState(false)
  const markLogoReady = useCallback(() => setLogoReady(true), [])
  const [pastDeadline, setPastDeadline] = useState(false)
  useEffect(() => {
    const timer = setTimeout(() => setPastDeadline(true), REVEAL_DEADLINE_MS)
    return () => clearTimeout(timer)
  }, [])
  const holding = !pastDeadline && (logo.pending || versionPending || (hasLogo && !logoReady))
  const shownVersion = version && version.environment !== 'prod' ? version : undefined
  return (
    <div className="grid min-h-svh lg:grid-cols-2">
      <aside className="bg-card hidden flex-col justify-between border-l p-10 lg:order-last lg:flex">
        <Wordmark className="text-2xl" />
        {shownVersion && <VersionNote version={shownVersion} />}
      </aside>

      <main className="grid place-items-center p-6">
        {/* Held invisible until everything above and below the fields has its final size (the
            settings read, the logo images, and the version note shown below lg): anything arriving
            after the form painted would move the fields under a user who is already typing. */}
        <div className="w-full max-w-sm">
          {/* Below lg the decorative aside is gone; keep the brand above the form and the version
              below it so both stay visible on mobile. Outside the hold: its size is final, and
              without it a phone shows a blank page for as long as the hold lasts. */}
          <Wordmark className="mb-8 text-xl lg:hidden" />
          <div className={cn(holding && 'invisible')}>
            {logo.light && logo.dark && (
              <ClientLogo
                light={logo.light}
                dark={logo.dark}
                alt=""
                onReady={markLogoReady}
                className="mb-6 max-h-[100px] max-w-[200px]"
              />
            )}
            {/* `role="status"` implies `aria-live="polite"`. Only announces a title that mutates
              after mount — one already in its final state on first render never announces this way. */}
            <header className="mb-6" role="status">
              <h1 className="font-display text-2xl font-semibold tracking-tight">{title}</h1>
              {subtitle && (
                <p id="auth-shell-subtitle" className="text-muted-foreground mt-1 text-sm">
                  {subtitle}
                </p>
              )}
            </header>
            {children}
            {/* Complementary, not part of the form: a screen reader on a phone can skip it instead of
              hearing it as primary content after the submit button. */}
            {shownVersion && (
              <aside aria-label="console version" className="mt-8 border-t pt-6 lg:hidden">
                <VersionNote version={shownVersion} />
              </aside>
            )}
          </div>
        </div>
      </main>
    </div>
  )
}
