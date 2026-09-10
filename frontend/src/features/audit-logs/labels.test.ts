import { describe, expect, it } from 'vitest'
import { humanizeAction, actionDomain, AUDIT_ACTIONS } from './labels'

describe('humanizeAction', () => {
  it('splits domain.verb into a readable "Domain · Verb" label', () => {
    expect(humanizeAction('evaluation_group.publish')).toBe('Evaluation group · Publish')
    expect(humanizeAction('ai_model.credential_set')).toBe('AI model · Credential set')
  })

  it('renders the "ai" segment as the AI acronym, not "Ai"', () => {
    expect(humanizeAction('ai_model.delete')).toBe('AI model · Delete')
  })

  it('humanizes a code with no dot', () => {
    expect(humanizeAction('login')).toBe('Login')
  })

  it('passes an empty string through', () => {
    expect(humanizeAction('')).toBe('')
  })
})

describe('AUDIT_ACTIONS', () => {
  it('lists the admin actions the users list can trigger, so the filter can select them', () => {
    expect(AUDIT_ACTIONS).toEqual(
      expect.arrayContaining(['user.force_logout', 'user.status_change', 'user.credential_reset']),
    )
  })

  it('labels them readably — the reset keeps the backend\'s "credential" wording', () => {
    expect(humanizeAction('user.status_change')).toBe('User · Status change')
    expect(humanizeAction('user.credential_reset')).toBe('User · Credential reset')
  })
})

describe('actionDomain', () => {
  it('returns the segment before the first dot, humanized', () => {
    expect(actionDomain('evaluation_group.publish')).toBe('Evaluation group')
    expect(actionDomain('data.read')).toBe('Data')
  })
})
