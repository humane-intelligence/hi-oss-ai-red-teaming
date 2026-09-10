import { describe, expect, it } from 'vitest'
import { objectHref } from './object-link'

describe('objectHref', () => {
  it('maps evaluation to its detail route', () => {
    expect(objectHref({ object_type: 'evaluation', object_id: 'e1' })).toBe('/evaluations/e1')
  })

  it('maps evaluation_group to its detail route', () => {
    expect(objectHref({ object_type: 'evaluation_group', object_id: 'g1' })).toBe(
      '/evaluation-groups/g1',
    )
  })

  it('maps ai_model to its detail route', () => {
    expect(objectHref({ object_type: 'ai_model', object_id: 'm1' })).toBe('/ai-models/m1')
  })

  it('returns null without an object_id', () => {
    expect(objectHref({ object_type: 'evaluation', object_id: null })).toBeNull()
    expect(objectHref({ object_type: null, object_id: null })).toBeNull()
  })

  it('returns null for a type with no route', () => {
    // @ts-expect-error — exercising the default branch with an unmapped type
    expect(objectHref({ object_type: 'mystery', object_id: 'x' })).toBeNull()
  })
})
