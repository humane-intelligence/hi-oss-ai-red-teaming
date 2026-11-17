import type { ReactNode } from 'react'
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
  const { data: version } = useVersion()
  const logo = useClientLogo()
  const shownVersion = version && version.environment !== 'prod' ? version : undefined
  return (
    <div className="grid min-h-svh lg:grid-cols-2">
      <aside className="bg-card hidden flex-col justify-between border-r p-10 lg:flex">
        <Wordmark className="text-2xl" />
        {shownVersion && <VersionNote version={shownVersion} />}
      </aside>

      <main className="grid place-items-center p-6">
        {/* Held invisible until the public settings settle: a logo arriving after the form painted
            would push the fields down under a user who is already typing. */}
        <div className={cn('w-full max-w-sm', logo.pending && 'invisible')}>
          {/* Below lg the decorative aside is gone; keep the brand above the form and the version
              below it so both stay visible on mobile. */}
          <Wordmark className="mb-8 text-xl lg:hidden" />
          {logo.light && logo.dark && (
            <ClientLogo
              light={logo.light}
              dark={logo.dark}
              alt=""
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
      </main>
    </div>
  )
}
