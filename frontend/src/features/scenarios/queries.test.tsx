import type { ReactNode } from 'react'
import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { server } from '@/test/msw/server'
import { useScenario } from './queries'

const EVAL_ID = 'eval-0001-0000-0000-000000000000'
const SCENARIO_ID = 'scen-0001-0000-0000-000000000000'
const SCENARIO_URL = `http://localhost/api/v1/evaluations/${EVAL_ID}/scenarios/${SCENARIO_ID}`

function withClient() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
}

describe('useScenario', () => {
  // `null` is load-bearing: three callers read it as "the scenario is gone" (a tombstone
  // note, a `required_reviews` fallback). Only a 404 may produce it.
  it('resolves to null on 404, the tombstone the callers render', async () => {
    server.use(
      http.get(SCENARIO_URL, () => HttpResponse.json({ detail: 'Not Found' }, { status: 404 })),
    )

    const { result } = renderHook(() => useScenario(EVAL_ID, SCENARIO_ID), {
      wrapper: withClient(),
    })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data).toBeNull()
  })

  it('errors on a 5xx instead of reporting the scenario as deleted', async () => {
    server.use(http.get(SCENARIO_URL, () => HttpResponse.json({ detail: 'Boom' }, { status: 500 })))

    const { result } = renderHook(() => useScenario(EVAL_ID, SCENARIO_ID), {
      wrapper: withClient(),
    })

    await waitFor(() => expect(result.current.isError).toBe(true))
    expect(result.current.data).toBeUndefined()
  })
})
