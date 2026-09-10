import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('sonner', () => ({ toast: { error: vi.fn() } }))

import { toast } from 'sonner'
import { notifyMutationError, queryClient } from './query'
import { ApiError, CONSENT_REQUIRED_TYPE } from './api/problem'

describe('notifyMutationError', () => {
  beforeEach(() => vi.clearAllMocks())

  it('suppresses the toast for a 400 carrying field errors[] (mapped inline by the form)', () => {
    notifyMutationError(
      new ApiError(
        { errors: [{ loc: ['body', 'password'], msg: 'too common', type: 'password_too_common' }] },
        400,
      ),
    )
    expect(toast.error).not.toHaveBeenCalled()
  })

  it('suppresses the toast for a 401 and for a 422', () => {
    notifyMutationError(new ApiError({}, 401))
    notifyMutationError(
      new ApiError({ errors: [{ loc: ['body', 'x'], msg: 'r', type: 'missing' }] }, 422),
    )
    expect(toast.error).not.toHaveBeenCalled()
  })

  it('toasts an error without field detail (e.g. a 409 conflict)', () => {
    notifyMutationError(new ApiError({ detail: 'conflict' }, 409))
    expect(toast.error).toHaveBeenCalledTimes(1)
  })

  it('suppresses the toast for the terms refusal, which the acceptance gate handles', () => {
    // It arrives on the write that published the version, so a toast would land under the success
    // one — and it is the same 403 a permission refusal uses, which must still toast.
    notifyMutationError(new ApiError({ type: CONSENT_REQUIRED_TYPE }, 403))

    expect(toast.error).not.toHaveBeenCalled()
  })

  it('still toasts a permission refusal, which nothing else reports', () => {
    notifyMutationError(new ApiError({ detail: "Caller lacks the 'x:y' permission" }, 403))

    expect(toast.error).toHaveBeenCalledTimes(1)
  })
})

describe('mutation error channel', () => {
  beforeEach(() => vi.clearAllMocks())

  it('skips the toast for a mutation that renders its own failures', () => {
    const onError = queryClient.getMutationCache().config.onError
    const mutation = { meta: { suppressErrorToast: true } } as never

    onError?.(new ApiError({ detail: 'boom' }, 409), undefined, undefined, mutation, {} as never)

    expect(toast.error).not.toHaveBeenCalled()
  })

  it('still toasts a mutation that does not opt out', () => {
    const onError = queryClient.getMutationCache().config.onError
    const mutation = { meta: undefined } as never

    onError?.(new ApiError({ detail: 'boom' }, 409), undefined, undefined, mutation, {} as never)

    expect(toast.error).toHaveBeenCalled()
  })
})
