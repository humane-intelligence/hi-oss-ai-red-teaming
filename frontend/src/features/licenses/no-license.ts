import type { DataLicenseSummary } from '@/lib/api/types'

// The catalog's "no license" entry. The server names it (`is_no_license`, derived from the catalog's
// own key) rather than the console recognising it by the absence of an `spdx_id` — a shape a second
// catalog entry, or a user-authored row whose author was hard-deleted, would also take.
export function findNoLicense(items: DataLicenseSummary[]): DataLicenseSummary | undefined {
  return items.find((license) => license.is_no_license)
}
