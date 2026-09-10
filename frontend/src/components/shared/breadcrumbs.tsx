import { Fragment } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { cn } from '@/lib/utils'

export type Crumb = { label: string; to?: string }

// An ordered list, not a row of spans: a screen reader then announces how deep the trail is and
// which entry is the page you are on, which a flat row cannot convey. The separator stays
// decorative — it is punctuation, not content.
export function Breadcrumbs({
  items,
  label = 'Breadcrumb',
  nowrap = false,
}: {
  items: Crumb[]
  label?: string
  // For a trail in fixed-height chrome: wrapping there changes the chrome's height instead of the
  // trail's, so the caller clips rather than wraps.
  nowrap?: boolean
}) {
  const { pathname } = useLocation()
  // `aria-current="page"` marks the crumb that *is* this page — not simply the last one. A leaf that
  // links elsewhere (the group crumb on a child page) is a way out, not where you are, so the test
  // is the destination rather than the position; an unlinked leaf is the page by construction.
  const isCurrent = (crumb: Crumb, index: number) =>
    crumb.to ? crumb.to === pathname : index === items.length - 1

  return (
    <nav aria-label={label} className="text-muted-foreground text-sm">
      <ol
        className={cn(
          'flex items-center gap-1.5',
          nowrap ? 'flex-nowrap whitespace-nowrap' : 'flex-wrap',
        )}
      >
        {items.map((c, i) => (
          <Fragment key={i}>
            {i > 0 && (
              <li aria-hidden className="text-muted-foreground/50">
                /
              </li>
            )}
            {/* Under `nowrap` the entries shrink and end in an ellipsis rather than being cut
                mid-word: a clipped name reads as a rendering fault, an ellipsis reads as a name that
                continues. The section root keeps its full width — it is short, and it is the anchor a
                reader scans for — so only the entity names give way. */}
            <li className={cn(nowrap && (i === 0 ? 'shrink-0' : 'min-w-0'))}>
              {c.to ? (
                <Link
                  to={c.to}
                  aria-current={isCurrent(c, i) ? 'page' : undefined}
                  className={cn(
                    'hover:text-foreground underline-offset-2 hover:underline',
                    nowrap && 'block truncate',
                  )}
                >
                  {c.label}
                </Link>
              ) : (
                <span
                  aria-current={isCurrent(c, i) ? 'page' : undefined}
                  className={cn('text-foreground', nowrap && 'block truncate')}
                >
                  {c.label}
                </span>
              )}
            </li>
          </Fragment>
        ))}
      </ol>
    </nav>
  )
}
