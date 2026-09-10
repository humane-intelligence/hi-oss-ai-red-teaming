import type { DataLicenseSummary } from '@/lib/api/types'

// Deterministic id from the full spdx id (djb2 → hex), so two stubs of equal-length spdx ids don't
// collide; tests that need a specific id pass one via `overrides.id`.
function stubId(spdxId: string): string {
  let h = 5381
  for (let i = 0; i < spdxId.length; i++) h = (h * 33 + spdxId.charCodeAt(i)) >>> 0
  return `00000000-0000-0000-0000-${h.toString(16).padStart(12, '0')}`
}

// A resolved-license stub for test mocks — the shape an `effective_license` field now carries.
export function licenseStub(
  spdxId = 'CC-BY-4.0',
  overrides: Partial<DataLicenseSummary> = {},
): DataLicenseSummary {
  return {
    id: stubId(spdxId),
    spdx_id: spdxId,
    name: spdxId,
    version: null,
    short_description: '',
    reference_url: `https://example.test/${spdxId}`,
    is_curated: true,
    is_default: spdxId === 'CC-BY-4.0',
    has_content: false,
    is_no_license: false,
    text_managed_in_code: false,
    protects_conversation_data: false,
    created_by_id: null,
    ...overrides,
  }
}

// The catalog's "no license" entry, as the server projects it: flagged, and publishing no SPDX id.
export function noLicenseStub(): DataLicenseSummary {
  return licenseStub('NONE', {
    name: 'No license',
    spdx_id: null,
    reference_url: null,
    has_content: true,
    is_no_license: true,
    text_managed_in_code: true,
  })
}
