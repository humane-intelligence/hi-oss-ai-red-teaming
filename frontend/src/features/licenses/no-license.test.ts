import { describe, expect, it } from 'vitest'
import { findNoLicense } from './no-license'
import { licenseStub, noLicenseStub } from './test-fixtures'

describe('findNoLicense', () => {
  it('finds the entry the server flags', () => {
    const sentinel = noLicenseStub()

    expect(findNoLicense([licenseStub('CC-BY-4.0'), sentinel])?.id).toBe(sentinel.id)
  })

  it('does not mistake a curated row that merely publishes no SPDX id for it', () => {
    // The shape the console used to match on — curated, `spdx_id: null` — is reachable by a second
    // catalog entry, and by a user-authored row whose author was hard-deleted (`created_by_id` is
    // `ON DELETE SET NULL`, and `is_curated` is exactly that column being null). Only the server's
    // flag distinguishes them, so seeding a private group can no longer land on the wrong licence.
    const lookalike = licenseStub('OTHER', {
      spdx_id: null,
      is_curated: true,
      is_no_license: false,
    })

    expect(findNoLicense([lookalike])).toBeUndefined()
    expect(findNoLicense([lookalike, noLicenseStub()])?.name).toBe('No license')
  })
})
