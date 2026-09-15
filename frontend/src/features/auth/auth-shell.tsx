import type { ReactNode } from 'react'
import { useVersion } from '@/features/status/queries'
import type { VersionResponse } from '@/lib/api/types'
import { cn } from '@/lib/utils'

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

function Readout({ version }: { version?: VersionResponse }) {
  return (
    <dl className="space-y-2.5 font-mono text-xs">
      <ReadoutRow
        k="status"
        v={
          <>
            {/* Decorative: "operational" already carries the state in text, so the glyph is hidden
                rather than announced as "black circle" (same call as backend-status.tsx). */}
            <span aria-hidden="true" className="text-ok">
              ●
            </span>{' '}
            operational
          </>
        }
      />
      {/* A state, like the two rows around it — an instruction ("authenticate to continue") is only
          true of /login, and this block now renders under the form on every auth route. */}
      <ReadoutRow k="access" v="unauthenticated" />
      {/* SHA is public info, but only advertise it off prod. */}
      {version && version.environment !== 'prod' && <ReadoutRow k="version" v={version.version} />}
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
  return (
    <div className="grid min-h-svh lg:grid-cols-2">
      <aside className="bg-card hidden flex-col justify-between border-r p-10 lg:flex">
        <Wordmark className="text-2xl" />
        <Readout version={version} />
      </aside>

      <main className="grid place-items-center p-6">
        <div className="w-full max-w-sm">
          {/* Below lg the decorative aside is gone; keep the brand above the form and the
              status/access/version readout below it so both stay visible on mobile. */}
          <Wordmark className="mb-8 text-xl lg:hidden" />
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
          <aside aria-label="console status" className="mt-8 border-t pt-6 lg:hidden">
            <Readout version={version} />
          </aside>
        </div>
      </main>
    </div>
  )
}
