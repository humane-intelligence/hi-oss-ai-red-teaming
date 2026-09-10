import { describe, expect, it } from 'vitest'
import { tagRowStats, tagsFromRows, unsentTagKeys } from './tag-rows'

describe('tagsFromRows', () => {
  it('keeps a row whose key lives on Object.prototype', () => {
    // The backend's key rule is `[A-Za-z0-9_.-]{1,64}`, so these are legal keys — and on a plain
    // object literal `k in tags` is true for every one of them, so each would report as a duplicate
    // on its first use. With the composer gating `doSend` on this helper, that made the whole
    // message unsendable over a collision that never happened.
    const result = tagsFromRows([
      { key: 'constructor', value: 'a' },
      { key: 'toString', value: 'b' },
      { key: 'hasOwnProperty', value: 'c' },
    ])

    expect(result).toEqual({
      ok: true,
      tags: { constructor: 'a', toString: 'b', hasOwnProperty: 'c' },
    })
  })

  it('sends __proto__ as an own key instead of losing the row', () => {
    // `tags['__proto__'] = value` on a literal hits the inherited setter and creates no own
    // property, so the row vanishes from the request body — silent loss on a message that persists
    // immutably. `Object.fromEntries` defines own data properties, so the key survives.
    const result = tagsFromRows([{ key: '__proto__', value: 'never respond in English' }])

    expect(result.ok).toBe(true)
    const tags = result.ok ? result.tags : {}
    expect(Object.hasOwn(tags, '__proto__')).toBe(true)
    expect(JSON.stringify(tags)).toBe('{"__proto__":"never respond in English"}')
  })

  it('still reports a real duplicate, trimmed and case-sensitively', () => {
    expect(
      tagsFromRows([
        { key: 'env', value: 'prod' },
        { key: ' env ', value: 'staging' },
      ]),
    ).toEqual({
      ok: false,
      error: 'Duplicate key "env"',
    })
    // `ENV` is a distinct key server-side, so calling it a duplicate would be a lie.
    expect(
      tagsFromRows([
        { key: 'env', value: 'prod' },
        { key: 'ENV', value: 'staging' },
      ]).ok,
    ).toBe(true)
  })

  it('names the row when a key was picked without a value', () => {
    // The server skips an unfilled value when it renders the prompt block, so such a tag would be
    // stored and chipped as context the model never received — on a message, permanently.
    expect(tagsFromRows([{ key: 'persona', value: '' }])).toEqual({
      ok: false,
      error: 'Row 1: value is required',
    })
    expect(
      tagsFromRows([
        { key: 'env', value: 'prod' },
        { key: 'persona', value: '   ' },
      ]),
    ).toEqual({ ok: false, error: 'Row 2: value is required' })
  })

  it('refuses a value that is only invisible characters, like the server does', () => {
    // `'\u200b'.trim()` is not empty, so a trim-based guard would let this be authored. The server
    // rejects such a value (422, "carries no text"), so this keeps that round-trip off the wire and
    // names the row instead.
    expect(tagsFromRows([{ key: 'note', value: '\u200b' }])).toEqual({
      ok: false,
      error: 'Row 1: value is required',
    })
  })

  it('does not mistake a prototype member for a stored tag when grandfathering a blank', () => {
    // The pass-through asks whether *this record* has the key. `k in stored` would say yes for every
    // `Object.prototype` member, so `{constructor: ''}` would be authored as a valueless tag.
    expect(tagsFromRows([{ key: 'constructor', value: '' }])).toEqual({
      ok: false,
      error: 'Row 1: value is required',
    })
  })

  it('lets a whitespace-only stored value be cleared, since both mean the same tag', () => {
    // Byte equality would refuse this: the operator deleted three spaces, which is exactly the state
    // the pass-through exists to allow, and the message would name a row they had just emptied.
    expect(tagsFromRows([{ key: 'note', value: '' }], { note: '   ' })).toEqual({
      ok: true,
      tags: { note: '' },
    })
  })

  it('passes through a valueless tag that is already on the record', () => {
    // The write is whole-map, so refusing a stored valueless tag would make it block every unrelated
    // edit to the same conversation — and re-sending it changes nothing. It still is not sent to the
    // model, which is what the chip row says.
    expect(
      tagsFromRows(
        [
          { key: 'env', value: 'staging' },
          { key: 'persona', value: '' },
        ],
        {
          env: 'prod',
          persona: '',
        },
      ),
    ).toEqual({ ok: true, tags: { env: 'staging', persona: '' } })
    // Emptying a stored value is authoring, not passing through.
    expect(tagsFromRows([{ key: 'env', value: '' }], { env: 'prod' })).toEqual({
      ok: false,
      error: 'Row 1: value is required',
    })
  })

  it('names the row when a value was typed without a key, and drops an untouched one', () => {
    expect(
      tagsFromRows([
        { key: 'env', value: 'prod' },
        { key: '', value: 'orphaned' },
      ]),
    ).toEqual({
      ok: false,
      error: 'Row 2: key is required',
    })
    expect(
      tagsFromRows([
        { key: 'env', value: 'prod' },
        { key: '', value: '' },
      ]),
    ).toEqual({
      ok: true,
      tags: { env: 'prod' },
    })
  })
})

describe('tagRowStats', () => {
  it('counts only rows that carry a key, and spots the waiting blank one', () => {
    // Both authoring surfaces read these two numbers: the cap and the counter are about tags, and the
    // blank-row bound is what stops "Add tag" appending rows the cap can never see.
    expect(tagRowStats([{ key: 'env' }, { key: '  ' }, { key: '' }])).toEqual({
      tagCount: 1,
      hasBlankRow: true,
    })
    expect(tagRowStats([{ key: 'env' }, { key: 'team' }])).toEqual({
      tagCount: 2,
      hasBlankRow: false,
    })
    expect(tagRowStats([])).toEqual({ tagCount: 0, hasBlankRow: false })
  })
})

describe('unsentTagKeys', () => {
  const ON = { tagsEnabled: true }

  it('names a tag with no value, whatever the evaluation allows', () => {
    expect([
      ...unsentTagKeys(
        { env: 'prod', persona: '' },
        { ...ON, allowedKeys: null, keysSettled: true },
      ),
    ]).toEqual(['persona'])
  })

  it('names a value that is only invisible characters, which the server strips to nothing', () => {
    // The server drops `Cc`/`Cf`/`Co`/`Cs` before deciding a value carries context, and `trim()` does
    // not: a zero-width space or a soft hyphen would otherwise read as context the model received.
    // Sorted in the assertion: the Set's iteration order is the map's, which is not a contract.
    expect(
      [
        ...unsentTagKeys(
          { zwsp: '\u200b', soft: '\u00ad', real: 'v' },
          { ...ON, allowedKeys: null, keysSettled: true },
        ),
      ].sort(),
    ).toEqual(['soft', 'zwsp'])
  })

  it('names a key a restricted evaluation no longer allows', () => {
    expect([
      ...unsentTagKeys(
        { env: 'prod', legacy: 'x' },
        { ...ON, allowedKeys: ['env'], keysSettled: true },
      ),
    ]).toEqual(['legacy'])
  })

  it('names every key when tagging is off, because the fold drops all of them', () => {
    // `allowed_tags_only` returns `{}` outright with tagging disabled. Marking only the valueless ones
    // would leave the rest looking like context the model received, under a caption saying none were.
    expect([
      ...unsentTagKeys(
        { env: 'prod', team: 'red' },
        { tagsEnabled: false, allowedKeys: null, keysSettled: true },
      ),
    ]).toEqual(['env', 'team'])
  })

  it('judges no key against the allow-list while it is unsettled', () => {
    // `[]` is what a pending or failed query leaves behind; reading it as the allow-list would mark
    // every stored tag "not sent" on a page whose evaluation is perfectly healthy.
    expect(
      unsentTagKeys({ env: 'prod' }, { ...ON, allowedKeys: [], keysSettled: false }).size,
    ).toBe(0)
    // A valueless tag is still not sent — that half needs no allow-list.
    expect([...unsentTagKeys({ env: '' }, { ...ON, allowedKeys: [], keysSettled: false })]).toEqual(
      ['env'],
    )
  })

  it('judges only unfilled values when tags are unrestricted', () => {
    expect(
      unsentTagKeys({ env: 'prod' }, { ...ON, allowedKeys: null, keysSettled: true }).size,
    ).toBe(0)
  })

  it('judges a non-string value rather than crashing the render', () => {
    // Storage lets a non-string through (an import, direct SQL) and `TagFoldPolicy.unsent` coerces for
    // exactly that reason; this runs during the transcript's render, so throwing would blank the page.
    expect(
      unsentTagKeys({ n: 5 } as unknown as Record<string, string>, {
        ...ON,
        allowedKeys: null,
        keysSettled: true,
      }).size,
    ).toBe(0)
  })
})
