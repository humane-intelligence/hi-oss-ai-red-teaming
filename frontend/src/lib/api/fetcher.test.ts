import { describe, expect, it } from 'vitest'
import { unwrap } from './fetcher'
import { ApiError } from './problem'

describe('unwrap', () => {
  it('returns data on a 2xx response', () => {
    const result = { data: { id: 1 }, response: new Response(null, { status: 200 }) }
    expect(unwrap(result)).toEqual({ id: 1 })
  })

  it('throws ApiError carrying status and problem on a non-2xx response', () => {
    const problem = { title: 'Bad Request', status: 400 }
    const result = { error: problem, response: new Response(null, { status: 400 }) }
    expect(() => unwrap(result)).toThrow(ApiError)
    try {
      unwrap(result)
    } catch (err) {
      expect(err).toBeInstanceOf(ApiError)
      expect((err as ApiError).status).toBe(400)
      expect((err as ApiError).problem).toEqual(problem)
    }
  })

  it('throws when error is set even if the response is ok', () => {
    const result = { error: {}, response: new Response(null, { status: 200 }) }
    expect(() => unwrap(result)).toThrow(ApiError)
  })

  it('defaults problem to {} when error is undefined on a failed response', () => {
    const result = { response: new Response(null, { status: 500 }) }
    try {
      unwrap(result)
    } catch (err) {
      expect((err as ApiError).status).toBe(500)
      expect((err as ApiError).problem).toEqual({})
    }
  })
})
