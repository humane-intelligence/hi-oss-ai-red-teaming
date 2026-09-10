import { describe, expect, it } from 'vitest'
import { memberA11yLabel, memberLabel, memberNameSuffixes } from './member-names'

describe('memberNameSuffixes', () => {
  it('leaves distinct names unsuffixed', () => {
    expect(memberNameSuffixes([{ modelName: 'gpt-4o' }, { modelName: 'claude-sonnet-4' }])).toEqual(
      ['', ''],
    )
  })

  it('numbers every member sharing a masked name', () => {
    expect(
      memberNameSuffixes([
        { modelName: '— masked —' },
        { modelName: '— masked —' },
        { modelName: '— masked —' },
      ]),
    ).toEqual([' (1)', ' (2)', ' (3)'])
  })

  it('numbers by group position, not by rank among the duplicates', () => {
    // `(3)` must be the third pane — that is what makes the number mappable to the row
    // of panes on screen.
    expect(
      memberNameSuffixes([
        { modelName: 'gpt-4o' },
        { modelName: 'claude-sonnet-4' },
        { modelName: 'gpt-4o' },
      ]),
    ).toEqual([' (1)', '', ' (3)'])
  })

  it('identifies a member by its title when it has one', () => {
    // Two conversations on the same model are distinct once titled; two sharing a title
    // are not, even on different models.
    expect(
      memberNameSuffixes([
        { title: 'Refusal probe', modelName: 'gpt-4o' },
        { title: 'Jailbreak', modelName: 'gpt-4o' },
      ]),
    ).toEqual(['', ''])
    expect(
      memberNameSuffixes([
        { title: 'Refusal probe', modelName: 'gpt-4o' },
        { title: 'Refusal probe', modelName: 'claude-sonnet-4' },
      ]),
    ).toEqual([' (1)', ' (2)'])
  })

  it('treats an empty title as no title', () => {
    expect(
      memberNameSuffixes([
        { title: '', modelName: '— masked —' },
        { title: null, modelName: '— masked —' },
      ]),
    ).toEqual([' (1)', ' (2)'])
  })

  it('returns nothing for an empty group', () => {
    expect(memberNameSuffixes([])).toEqual([])
  })
})

describe('memberLabel / memberA11yLabel', () => {
  it('prefers the title on screen but keeps the model for screen readers', () => {
    const member = { title: 'Refusal probe', modelName: 'gpt-4o' }
    expect(memberLabel(member)).toBe('Refusal probe')
    expect(memberA11yLabel(member)).toBe('Refusal probe (gpt-4o)')
  })

  it('falls back to the model when there is no title', () => {
    expect(memberLabel({ modelName: 'gpt-4o' })).toBe('gpt-4o')
    expect(memberA11yLabel({ modelName: 'gpt-4o' })).toBe('gpt-4o')
  })

  it('keeps the visible label a prefix of the accessible name when both a title and a suffix apply', () => {
    // WCAG 2.5.3: voice control matches the visible label against the accessible name, so
    // `Refusal probe (2)` must not become `Refusal probe (gpt-4o) (2)`.
    const member = { title: 'Refusal probe', modelName: 'gpt-4o' }
    expect(memberLabel(member, ' (2)')).toBe('Refusal probe (2)')
    expect(memberA11yLabel(member, ' (2)')).toBe('Refusal probe (2) (gpt-4o)')
    expect(memberA11yLabel(member, ' (2)').startsWith(memberLabel(member, ' (2)'))).toBe(true)
  })

  it('appends the suffix to both, so a row and its pane read alike', () => {
    const member = { title: null, modelName: '— masked —' }
    expect(memberLabel(member, ' (2)')).toBe('— masked — (2)')
    expect(memberA11yLabel(member, ' (2)')).toBe('— masked — (2)')
  })
})
