// Loading placeholder for detail pages, matching the DataTable skeleton's feel.
export function DetailSkeleton() {
  return (
    <div className="space-y-4" data-testid="detail-skeleton">
      <div className="bg-muted h-7 w-48 animate-pulse rounded" />
      <div className="bg-muted h-4 w-full max-w-md animate-pulse rounded" />
      <div className="bg-muted h-40 w-full animate-pulse rounded-lg" />
    </div>
  )
}
