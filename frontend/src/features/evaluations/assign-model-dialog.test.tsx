import type { ReactElement } from 'react'
import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { openSelect, setSelect } from '@/test/select'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { AssignModelDialog } from './assign-model-dialog'

const EVAL_ID = 'eval-0001-0000-0000-000000000000'
const MODEL_ID = 'model-001-0000-0000-000000000000'
const ASSIGN_ID = 'asgn-0001-0000-0000-000000000000'

function aiModelsHandler(overrides: Record<string, unknown> = {}) {
  return http.get('http://localhost/api/v1/ai-models', () =>
    HttpResponse.json({
      items: [{ id: MODEL_ID, name: 'gpt-4o', model_alias: 'gpt-4o', labels: [], ...overrides }],
      total: 1,
      limit: 100,
      offset: 0,
    }),
  )
}

function assignmentResponse(parameters: Record<string, unknown>, mask: string | null = null) {
  return {
    id: ASSIGN_ID,
    evaluation_id: EVAL_ID,
    model_id: MODEL_ID,
    model_display_mask: mask,
    parameters,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  }
}

function renderDialog(ui: ReactElement, permissions = ['evaluations:update', 'models:read']) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(permissions)
  return render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter>{ui}</MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

describe('AssignModelDialog — assign (create)', () => {
  // `FormField` cannot clone aria props onto a `<Controller>`, so this pair is wired by hand and
  // needs a test: without it the trigger loses its error styling and the reason is never announced.
  it('marks the model trigger invalid and points it at the error', async () => {
    const user = userEvent.setup()
    renderDialog(<AssignModelDialog evaluationId={EVAL_ID} open onOpenChange={() => {}} />)

    const trigger = await screen.findByLabelText('Model')
    expect(trigger).not.toHaveAttribute('aria-invalid')

    await user.click(screen.getByRole('button', { name: /^assign$/i }))

    await waitFor(() => expect(trigger).toHaveAttribute('aria-invalid', 'true'))
    const describedBy = trigger.getAttribute('aria-describedby')
    expect(describedBy).toBeTruthy()
    expect(document.getElementById(describedBy!)).toHaveTextContent('Select a model')
  })

  it('sends parameters when Advanced fields are filled', async () => {
    let captured: unknown = null
    server.use(
      aiModelsHandler(),
      http.post(`http://localhost/api/v1/evaluations/${EVAL_ID}/models`, async ({ request }) => {
        captured = await request.json()
        return HttpResponse.json(assignmentResponse({ temperature: 0.7 }), { status: 201 })
      }),
    )

    const user = userEvent.setup()
    renderDialog(<AssignModelDialog evaluationId={EVAL_ID} open onOpenChange={() => {}} />)

    await setSelect('Model', /^gpt-4o/)
    await user.click(screen.getByRole('button', { name: /advanced model parameters/i }))
    await user.type(screen.getByLabelText('Temperature'), '0.7')
    await user.type(screen.getByLabelText('System prompt'), 'You are helpful.')
    await user.click(screen.getByRole('button', { name: 'Assign' }))

    await waitFor(() => expect(captured).not.toBeNull())
    const body = captured as {
      model_id: string
      parameters?: { temperature?: number; system_prompt?: string }
    }
    expect(body.model_id).toBe(MODEL_ID)
    expect(body.parameters?.temperature).toBe(0.7)
    expect(body.parameters?.system_prompt).toBe('You are helpful.')
  })

  it("names a model's labels in the option, so a fine-tuned one is not assigned by mistake", async () => {
    server.use(
      http.get('http://localhost/api/v1/ai-models', () =>
        HttpResponse.json({
          items: [
            {
              id: MODEL_ID,
              name: 'gpt-4o',
              model_alias: 'gpt-4o',
              labels: ['audited', 'fine-tuned', 'self-hosted'],
            },
            {
              id: 'model-002-0000-0000-000000000000',
              name: 'plain',
              model_alias: 'plain',
              labels: [],
            },
          ],
          total: 2,
          limit: 100,
          offset: 0,
        }),
      ),
    )

    renderDialog(<AssignModelDialog evaluationId={EVAL_ID} open onOpenChange={() => {}} />)

    // Labels are appended to the option text, summarised past two (the API sorts them), with the
    // full set on the title. The list is a popover, so it has to be opened first.
    await openSelect('Model')
    const option = await screen.findByRole('option', {
      name: 'gpt-4o (gpt-4o) — audited, fine-tuned +1',
    })
    expect(option).toBeInTheDocument()
    expect(option).toHaveAttribute('title', 'audited, fine-tuned, self-hosted')
    // An unlabelled model gains no dash, and no empty title.
    const plain = screen.getByRole('option', { name: 'plain (plain)' })
    expect(plain).toBeInTheDocument()
    expect(plain).not.toHaveAttribute('title')
  })

  it('omits parameters when no Advanced fields are set', async () => {
    let captured: unknown = null
    server.use(
      aiModelsHandler(),
      http.post(`http://localhost/api/v1/evaluations/${EVAL_ID}/models`, async ({ request }) => {
        captured = await request.json()
        return HttpResponse.json(assignmentResponse({}), { status: 201 })
      }),
    )

    const user = userEvent.setup()
    renderDialog(<AssignModelDialog evaluationId={EVAL_ID} open onOpenChange={() => {}} />)

    await setSelect('Model', /^gpt-4o/)
    await user.click(screen.getByRole('button', { name: 'Assign' }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect(Object.prototype.hasOwnProperty.call(captured, 'parameters')).toBe(false)
  })

  it('sends no parameters when a flagged model is picked after knobs were typed', async () => {
    // Create mode keeps the values react-hook-form holds for the unmounted panel, so the
    // guard has to be on submit — the panel disappearing is not enough.
    let captured: unknown = null
    server.use(
      http.get('http://localhost/api/v1/ai-models', () =>
        HttpResponse.json({
          items: [
            { id: MODEL_ID, name: 'gpt-4o', model_alias: 'gpt-4o', labels: [] },
            {
              id: 'model-002-0000-0000-000000000000',
              name: 'ignores-params',
              model_alias: 'ignores-params',
              labels: [],
              advanced_params_disabled: true,
            },
          ],
          total: 2,
          limit: 100,
          offset: 0,
        }),
      ),
      http.post(`http://localhost/api/v1/evaluations/${EVAL_ID}/models`, async ({ request }) => {
        captured = await request.json()
        return HttpResponse.json(assignmentResponse({}), { status: 201 })
      }),
    )

    const user = userEvent.setup()
    renderDialog(<AssignModelDialog evaluationId={EVAL_ID} open onOpenChange={() => {}} />)

    await setSelect('Model', /^gpt-4o/)
    await user.click(screen.getByRole('button', { name: /advanced model parameters/i }))
    await user.type(screen.getByLabelText('Temperature'), '0.7')

    // Switch to the model that ignores them: the panel goes away, and so must the payload.
    await setSelect('Model', /^ignores-params/)
    await waitFor(() =>
      expect(
        screen.queryByRole('button', { name: /advanced model parameters/i }),
      ).not.toBeInTheDocument(),
    )

    await user.click(screen.getByRole('button', { name: 'Assign' }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect(Object.prototype.hasOwnProperty.call(captured, 'parameters')).toBe(false)
  })

  it('lists only models assignable to the evaluation, even without global models:read', async () => {
    let modelsUrl: URL | undefined
    server.use(
      http.get('http://localhost/api/v1/ai-models', ({ request }) => {
        modelsUrl = new URL(request.url)
        return HttpResponse.json({
          items: [{ id: MODEL_ID, name: 'gpt-4o', model_alias: 'gpt-4o', labels: [] }],
          total: 1,
          limit: 100,
          offset: 0,
        })
      }),
    )
    // An in-group owner can assign (evaluations:update) but lacks the global models:read;
    // the picker still loads, authorized via the evaluation's group.
    renderDialog(<AssignModelDialog evaluationId={EVAL_ID} open onOpenChange={() => {}} />, [
      'evaluations:update',
    ])

    await openSelect('Model')
    expect(await screen.findByRole('option', { name: /gpt-4o/i })).toBeInTheDocument()
    await waitFor(() => expect(modelsUrl).toBeDefined())
    expect(modelsUrl!.searchParams.get('assignable_to_evaluation')).toBe(EVAL_ID)
  })
})

describe('AssignModelDialog — edit', () => {
  it('prefills the stored params and PATCHes the full set', async () => {
    let captured: unknown = null
    server.use(
      aiModelsHandler(),
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/models/${ASSIGN_ID}`, () =>
        HttpResponse.json(assignmentResponse({ temperature: 0.2 }, 'Model A')),
      ),
      http.patch(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/models/${ASSIGN_ID}`,
        async ({ request }) => {
          captured = await request.json()
          return HttpResponse.json(assignmentResponse({ temperature: 0.9 }, 'Model A'))
        },
      ),
    )

    const user = userEvent.setup()
    renderDialog(
      <AssignModelDialog
        evaluationId={EVAL_ID}
        open
        onOpenChange={() => {}}
        assignmentId={ASSIGN_ID}
        modelName="gpt-4o"
      />,
    )

    // Advanced panel auto-expands and the stored temperature is prefilled.
    await waitFor(() => expect(screen.getByLabelText('Temperature')).toHaveValue(0.2))
    expect(screen.getByLabelText('Display mask (optional)')).toHaveValue('Model A')

    await user.clear(screen.getByLabelText('Temperature'))
    await user.type(screen.getByLabelText('Temperature'), '0.9')
    await user.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(captured).not.toBeNull())
    const body = captured as {
      model_display_mask?: string | null
      parameters?: { temperature?: number }
    }
    expect(body.parameters?.temperature).toBe(0.9)
    expect(body.model_display_mask).toBe('Model A')
  })

  it('clears one knob back to the inherited value without touching the others', async () => {
    let captured: unknown = null
    server.use(
      aiModelsHandler(),
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/models/${ASSIGN_ID}`, () =>
        HttpResponse.json(assignmentResponse({ temperature: 0.2, top_p: 0.5 }, 'Model A')),
      ),
      http.patch(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/models/${ASSIGN_ID}`,
        async ({ request }) => {
          captured = await request.json()
          return HttpResponse.json(assignmentResponse({ top_p: 0.5 }, 'Model A'))
        },
      ),
    )

    const user = userEvent.setup()
    renderDialog(
      <AssignModelDialog
        evaluationId={EVAL_ID}
        open
        onOpenChange={() => {}}
        assignmentId={ASSIGN_ID}
        modelName="gpt-4o"
      />,
    )

    await waitFor(() => expect(screen.getByLabelText('Temperature')).toHaveValue(0.2))

    await user.click(screen.getByRole('button', { name: 'Clear Temperature' }))
    expect(screen.getByLabelText('Temperature')).toHaveValue(null)

    await user.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(captured).not.toBeNull())
    const body = captured as { parameters?: { temperature?: number; top_p?: number } }
    // Cleared knob is omitted so the layer below inherits; the untouched one survives.
    expect(body.parameters?.temperature).toBeUndefined()
    expect(body.parameters?.top_p).toBe(0.5)
  })

  it('offers no override panel when the catalog says the model ignores parameters', async () => {
    // Catalog only — no prop — so this fails if the authoritative branch is dropped.
    server.use(
      aiModelsHandler({ advanced_params_disabled: true }),
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/models/${ASSIGN_ID}`, () =>
        HttpResponse.json(assignmentResponse({}, 'Model A')),
      ),
    )

    renderDialog(
      <AssignModelDialog
        evaluationId={EVAL_ID}
        open
        onOpenChange={() => {}}
        assignmentId={ASSIGN_ID}
        modelName="gpt-4o"
      />,
    )

    await waitFor(() => expect(screen.getByText(/ignores advanced parameters/)).toBeInTheDocument())
    expect(
      screen.queryByRole('button', { name: /Advanced model parameters/ }),
    ).not.toBeInTheDocument()
  })

  it('falls back to the parent row flag when the catalog is unreadable', async () => {
    // Without models:read the picker query never runs, so the prop is the only source —
    // this fails if the fallback branch is dropped.
    server.use(
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/models/${ASSIGN_ID}`, () =>
        HttpResponse.json(assignmentResponse({}, 'Model A')),
      ),
    )

    renderDialog(
      <AssignModelDialog
        evaluationId={EVAL_ID}
        open
        onOpenChange={() => {}}
        assignmentId={ASSIGN_ID}
        modelName="gpt-4o"
        advancedParamsDisabled
      />,
      ['evaluations:update'],
    )

    await waitFor(() => expect(screen.getByText(/ignores advanced parameters/)).toBeInTheDocument())
  })

  it("keeps a flagged model's stored overrides instead of wiping them on save", async () => {
    // The panel is gone, so the form holds no knobs — omitting `parameters` leaves the
    // stored layer untouched, which is what the model form promises when re-enabling.
    let captured: Record<string, unknown> | null = null
    server.use(
      aiModelsHandler({ advanced_params_disabled: true }),
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/models/${ASSIGN_ID}`, () =>
        HttpResponse.json(assignmentResponse({ temperature: 0.2 }, 'Model A')),
      ),
      http.patch(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/models/${ASSIGN_ID}`,
        async ({ request }) => {
          captured = (await request.json()) as Record<string, unknown>
          return HttpResponse.json(assignmentResponse({ temperature: 0.2 }, 'Model A'))
        },
      ),
    )

    const user = userEvent.setup()
    renderDialog(
      <AssignModelDialog
        evaluationId={EVAL_ID}
        open
        onOpenChange={() => {}}
        assignmentId={ASSIGN_ID}
        modelName="gpt-4o"
      />,
    )

    await waitFor(() => expect(screen.getByText(/ignores advanced parameters/)).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect(Object.prototype.hasOwnProperty.call(captured, 'parameters')).toBe(false)
  })

  it('resolves the real model identity even when the masked row name is null', async () => {
    server.use(
      aiModelsHandler(),
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/models/${ASSIGN_ID}`, () =>
        HttpResponse.json(assignmentResponse({ temperature: 0.2 }, null)),
      ),
    )

    renderDialog(
      <AssignModelDialog
        evaluationId={EVAL_ID}
        open
        onOpenChange={() => {}}
        assignmentId={ASSIGN_ID}
        modelName={null}
      />,
    )

    // Masked read view gives a null name; the dialog resolves model_id -> name (alias).
    await waitFor(() => expect(screen.getByText('gpt-4o (gpt-4o)')).toBeInTheDocument())
  })

  it('preserves stored params the form does not manage', async () => {
    let captured: unknown = null
    server.use(
      aiModelsHandler(),
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/models/${ASSIGN_ID}`, () =>
        HttpResponse.json(
          assignmentResponse({ temperature: 0.2, stop_sequences: ['END'] }, 'Model A'),
        ),
      ),
      http.patch(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/models/${ASSIGN_ID}`,
        async ({ request }) => {
          captured = await request.json()
          return HttpResponse.json(assignmentResponse({ temperature: 0.9 }, 'Model A'))
        },
      ),
    )

    const user = userEvent.setup()
    renderDialog(
      <AssignModelDialog
        evaluationId={EVAL_ID}
        open
        onOpenChange={() => {}}
        assignmentId={ASSIGN_ID}
        modelName="gpt-4o"
      />,
    )

    await waitFor(() => expect(screen.getByLabelText('Temperature')).toHaveValue(0.2))
    await user.clear(screen.getByLabelText('Temperature'))
    await user.type(screen.getByLabelText('Temperature'), '0.9')
    await user.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(captured).not.toBeNull())
    const body = captured as {
      parameters?: { temperature?: number; stop_sequences?: string[] }
    }
    // Edited knob updates; the unmanaged stop_sequences is carried through, not dropped.
    expect(body.parameters?.temperature).toBe(0.9)
    expect(body.parameters?.stop_sequences).toEqual(['END'])
  })

  it('does not query the catalog when the caller lacks models:read', async () => {
    let aiModelsCalled = false
    server.use(
      http.get('http://localhost/api/v1/ai-models', () => {
        aiModelsCalled = true
        return HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 })
      }),
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/models/${ASSIGN_ID}`, () =>
        HttpResponse.json(assignmentResponse({ temperature: 0.2 }, 'Model A')),
      ),
    )

    renderDialog(
      <AssignModelDialog
        evaluationId={EVAL_ID}
        open
        onOpenChange={() => {}}
        assignmentId={ASSIGN_ID}
        modelName="Model A"
      />,
      ['evaluations:update'], // no models:read
    )

    // Params still prefill (the assignment GET needs only evaluations:update)...
    await waitFor(() => expect(screen.getByLabelText('Temperature')).toHaveValue(0.2))
    // ...the model shows the label fallback, and the catalog was never hit.
    expect(screen.getByText('Model A')).toBeInTheDocument()
    expect(aiModelsCalled).toBe(false)
  })

  it('clears all overrides by sending an empty parameters object', async () => {
    let captured: unknown = null
    server.use(
      aiModelsHandler(),
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/models/${ASSIGN_ID}`, () =>
        HttpResponse.json(assignmentResponse({ temperature: 0.2 }, 'Model A')),
      ),
      http.patch(
        `http://localhost/api/v1/evaluations/${EVAL_ID}/models/${ASSIGN_ID}`,
        async ({ request }) => {
          captured = await request.json()
          return HttpResponse.json(assignmentResponse({}, 'Model A'))
        },
      ),
    )

    const user = userEvent.setup()
    renderDialog(
      <AssignModelDialog
        evaluationId={EVAL_ID}
        open
        onOpenChange={() => {}}
        assignmentId={ASSIGN_ID}
        modelName="gpt-4o"
      />,
    )

    await waitFor(() => expect(screen.getByLabelText('Temperature')).toHaveValue(0.2))
    await user.clear(screen.getByLabelText('Temperature'))
    await user.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(captured).not.toBeNull())
    const body = captured as { parameters?: Record<string, unknown> }
    expect(body.parameters).toEqual({})
  })
})
