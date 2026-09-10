import { describe, expect, it } from 'vitest'
import {
  ApiError,
  CONSENT_REQUIRED_TYPE,
  fieldErrorsFromProblem,
  humanizeError,
  isConsentRequired,
} from './problem'

describe('ApiError', () => {
  it('prefers detail for the message', () => {
    const err = new ApiError({ detail: 'Boom', title: 'T' }, 400)
    expect(err.message).toBe('Boom')
    expect(err.status).toBe(400)
    expect(err.name).toBe('ApiError')
  })

  it('falls back to title, then to a generic message', () => {
    expect(new ApiError({ title: 'Only title' }, 404).message).toBe('Only title')
    expect(new ApiError({}, 503).message).toBe('Request failed (503)')
  })
})

describe('humanizeError', () => {
  it('maps common HTTP statuses to actionable text', () => {
    expect(humanizeError(new ApiError({}, 403))).toMatch(/permission/i)
    expect(humanizeError(new ApiError({}, 404))).toMatch(/not found/i)
    expect(humanizeError(new ApiError({}, 409))).toMatch(/conflicts/i)
    expect(humanizeError(new ApiError({}, 429))).toMatch(/too many/i)
    expect(humanizeError(new ApiError({}, 500))).toMatch(/server/i)
  })

  // 409 and 429 are the statuses this override actually changes — both have curated fallbacks
  // that would otherwise win. (400/410 already surface their detail via error.message.)
  it('surfaces the backend detail for 409 and 429', () => {
    expect(
      humanizeError(new ApiError({ detail: 'Model is already assigned to this evaluation.' }, 409)),
    ).toBe('Model is already assigned to this evaluation.')
    expect(humanizeError(new ApiError({ detail: 'Rate limit: 3 requests per second.' }, 429))).toBe(
      'Rate limit: 3 requests per second.',
    )
  })

  it('keeps curated copy for 403/404 even when they carry a noisy detail', () => {
    expect(
      humanizeError(new ApiError({ detail: "Caller lacks the 'x:y' permission" }, 403)),
    ).toMatch(/permission/i)
    expect(humanizeError(new ApiError({ detail: 'Model 3f2a-… not found.' }, 404))).toMatch(
      /not found/i,
    )
  })

  it('does not read the terms refusal as a permission problem', () => {
    // Same status, opposite meaning: one sends the reader to an administrator, the other to the
    // acceptance screen.
    const refusal = new ApiError(
      { type: CONSENT_REQUIRED_TYPE, detail: 'Accept the current terms of service to continue.' },
      403,
    )

    expect(humanizeError(refusal)).toMatch(/accept the current terms/i)
    expect(humanizeError(refusal)).not.toMatch(/permission/i)
    expect(isConsentRequired(refusal)).toBe(true)
    expect(
      isConsentRequired(new ApiError({ detail: "Caller lacks the 'x:y' permission" }, 403)),
    ).toBe(false)
  })

  it('keeps the generic message for a 5xx even when it carries a detail', () => {
    expect(humanizeError(new ApiError({ detail: 'raw server internals' }, 500))).toMatch(/server/i)
  })

  it('keeps the backend message for other 4xx', () => {
    expect(humanizeError(new ApiError({ detail: 'Bad title' }, 400))).toBe('Bad title')
  })

  it('explains network failures (TypeError)', () => {
    expect(humanizeError(new TypeError('Failed to fetch'))).toMatch(/reach the backend/i)
  })

  it('falls back to the message or a generic string', () => {
    expect(humanizeError(new Error('weird'))).toBe('weird')
    expect(humanizeError('not an error')).toBe('Request failed')
  })
})

describe('fieldErrorsFromProblem', () => {
  it('maps the last loc element to its message', () => {
    const problem = {
      errors: [
        { loc: ['body', 'email'], msg: 'invalid email', type: 'value_error' },
        { loc: ['body', 'password'], msg: 'too short', type: 'value_error' },
      ],
    }
    expect(fieldErrorsFromProblem(problem)).toEqual({
      email: 'invalid email',
      password: 'too short',
    })
  })

  it('ignores entries whose last loc element is not a string', () => {
    const problem = { errors: [{ loc: ['body', 0], msg: 'x', type: 't' }] }
    expect(fieldErrorsFromProblem(problem)).toEqual({})
  })

  it('returns an empty object when there are no errors', () => {
    expect(fieldErrorsFromProblem({})).toEqual({})
  })
})
