import { createPortal } from 'react-dom'
import { Link } from 'react-router-dom'
import { useGroupTrail } from './use-group-trail'
import { useNavTrailSlot } from '@/app/layout/nav-trail-slot'
import { Breadcrumbs, type Crumb } from '@/components/shared/breadcrumbs'
import { cn } from '@/lib/utils'

// The identity line — which organization and group a page sits in. It renders into the app header
// when the shell offers a slot, so the context stays in one predictable place instead of repeating
// down each page, and the page body starts one row higher.
//
// Without a slot it renders in place: that is what a page rendered on its own does (every page test),
// and it keeps the line reachable if the header ever stops carrying it.
//
// With `path` the line is the whole way down to the page — group, then the given crumbs — and drops
// the section root and the organization: an organization owns the group, it is not a level between
// the list and it, and on a page two levels below the group the reader wants the levels they are in.
export function GroupIdentity({ groupId, path }: { groupId: string; path?: Crumb[] }) {
  const trail = useGroupTrail(groupId)
  const { isPending } = trail
  const crumbs = path ? [...(trail.groupCrumb ? [trail.groupCrumb] : []), ...path] : trail.crumbs
  const slot = useNavTrailSlot()
  // Same type scale as the trail it stands in for, so the line keeps its height. Hidden above the
  // handover only where the header carries the line — without a slot the page is the only place it
  // can appear, and hiding it there would let the trail pop in instead of holding its row.
  // A path trail has crumbs of its own to show before the group resolves; replacing them with a
  // placeholder would blank the line on every deep link.
  if (isPending && !path)
    return <p className={cn('text-muted-foreground text-sm', slot && 'lg:hidden')}>Loading…</p>
  const inPage = <Breadcrumbs items={crumbs} label="Evaluation group" />
  if (!slot) return inPage
  const pinFirst = !path
  // Complementary, never both visible. `lg` not `md`: at 768px the organization and group crumbs
  // each measure 0px, because the section root keeps its full 120px and the slot has nothing left.
  return (
    <>
      <div className="lg:hidden">{inPage}</div>
      {createPortal(
        <div className="hidden overflow-hidden lg:block">
          <Breadcrumbs items={crumbs} label="Evaluation group" nowrap pinFirst={pinFirst} />
        </div>,
        slot,
      )}
    </>
  )
}

// The group's own page: the identity is the heading, the way back to the list sits above it.
//
// The header slot is filled here too, so the top bar carries the same context on every page of the
// subtree instead of emptying out on this one. That does put the organization on screen twice — the
// deliberate trade for a bar that never blinks — so the two landmarks take different names: a screen
// reader user should be able to tell the header's context line from the link back to the list.
export function GroupIdentityHeading({ groupId }: { groupId: string }) {
  const { crumbs, org, groupTitle } = useGroupTrail(groupId)
  const slot = useNavTrailSlot()
  return (
    <>
      {slot &&
        createPortal(
          <div className="hidden overflow-hidden lg:block">
            <Breadcrumbs items={crumbs} label="Evaluation group" nowrap />
          </div>,
          slot,
        )}
      {/* Rendered from the first frame, at its final type scale: an `h1` that appears only once the
          names land moves everything below it, and leaves the page with no heading until then. */}
      <h1 className="font-display flex min-w-0 flex-wrap items-center gap-x-1.5 text-2xl font-semibold tracking-tight">
        {org && (
          <>
            <Link to={org.to} className="text-muted-foreground hover:text-foreground font-normal">
              {org.name}
            </Link>
            {/* Full strength, not the breadcrumb's `/50`: at heading size this slash is the only
                boundary between two entity names. It is decorative, so the accessible name gets its
                own separator — a comma, because punctuation announcement is a verbosity setting
                while a comma reliably produces a pause. */}
            <span aria-hidden className="text-muted-foreground font-normal">
              /
            </span>
            <span className="sr-only">, </span>
          </>
        )}
        <span className={groupTitle ? undefined : 'text-muted-foreground'}>
          {groupTitle ?? 'Loading…'}
        </span>
      </h1>
    </>
  )
}
