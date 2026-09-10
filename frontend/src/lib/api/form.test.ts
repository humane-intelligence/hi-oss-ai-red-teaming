import { describe, expect, it, vi } from 'vitest'
import type { FieldValues, UseFormSetError } from 'react-hook-form'
import { applyApiError } from './form'
import { ApiError } from './problem'

describe('applyApiError', () => {
  it('returns false and does not touch the form for a non-ApiError', () => {
    const setError = vi.fn<UseFormSetError<FieldValues>>()
    expect(applyApiError(new Error('network'), setError)).toBe(false)
    expect(setError).not.toHaveBeenCalled()
  })

  it('returns false for an ApiError without field errors', () => {
    const setError = vi.fn<UseFormSetError<FieldValues>>()
    expect(applyApiError(new ApiError({ title: 'Server' }, 500), setError)).toBe(false)
    expect(setError).not.toHaveBeenCalled()
  })

  it('maps field errors from a non-422 ApiError (e.g. 400 password policy) and returns true', () => {
    const setError = vi.fn<UseFormSetError<FieldValues>>()
    const err = new ApiError(
      { errors: [{ loc: ['body', 'password'], msg: 'too common', type: 'password_too_common' }] },
      400,
    )
    expect(applyApiError(err, setError)).toBe(true)
    expect(setError).toHaveBeenCalledWith('password', { message: 'too common' })
  })

  it('maps 422 field errors onto the form and returns true', () => {
    const setError = vi.fn<UseFormSetError<FieldValues>>()
    const err = new ApiError(
      { errors: [{ loc: ['body', 'email'], msg: 'invalid email', type: 'value_error' }] },
      422,
    )
    expect(applyApiError(err, setError)).toBe(true)
    expect(setError).toHaveBeenCalledWith('email', { message: 'invalid email' })
  })

  it('returns false for a 422 with no field errors', () => {
    const setError = vi.fn<UseFormSetError<FieldValues>>()
    expect(applyApiError(new ApiError({}, 422), setError)).toBe(false)
    expect(setError).not.toHaveBeenCalled()
  })

  it('maps a whitelisted field', () => {
    const setError = vi.fn<UseFormSetError<FieldValues>>()
    const err = new ApiError(
      { errors: [{ loc: ['body', 'first_name'], msg: 'too long', type: 'string_too_long' }] },
      422,
    )
    expect(applyApiError(err, setError, ['first_name', 'last_name'])).toBe(true)
    expect(setError).toHaveBeenCalledWith('first_name', { message: 'too long' })
  })

  it('skips a field outside the whitelist so the caller can fall back', () => {
    const setError = vi.fn<UseFormSetError<FieldValues>>()
    const err = new ApiError(
      { errors: [{ loc: ['body', 'role_ids'], msg: 'not permitted', type: 'extra_forbidden' }] },
      422,
    )
    expect(applyApiError(err, setError, ['first_name', 'last_name'])).toBe(false)
    expect(setError).not.toHaveBeenCalled()
  })
})
