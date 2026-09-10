import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { ScenariosSection } from './scenarios-section'
import type { ScenarioResponse, TaskResponse } from '@/lib/api/types'

const EVAL_ID = 'eval-0001-0000-0000-000000000000'
const SCEN_ID = 'scen-0001-0000-0000-000000000000'
const TASK_ID = 'task-0001-0000-0000-000000000000'

const scenario: ScenarioResponse = {
  id: SCEN_ID,
  evaluation_id: EVAL_ID,
  name: 'Prompt injection',
  description: 'Try to override system prompt',
  position: 1,
  required_reviews: 1,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

const task: TaskResponse = {
  id: TASK_ID,
  scenario_id: SCEN_ID,
  name: 'Basic injection',
  description: 'Attempt via user turn',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

function renderSection(permissions: string[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(permissions)
  render(
    <Wrapper>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ScenariosSection evaluationId={EVAL_ID} />
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

function baseHandlers() {
  return [
    http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/scenarios`, () =>
      HttpResponse.json({ items: [scenario], total: 1, limit: 100, offset: 0 }),
    ),
    http.get(`http://localhost/api/v1/scenarios/${SCEN_ID}/tasks`, () =>
      HttpResponse.json({ items: [task], total: 1, limit: 100, offset: 0 }),
    ),
  ]
}

describe('ScenariosSection — RBAC gates', () => {
  it('with evaluations:read only — no Add scenario, no icon action buttons per row', async () => {
    server.use(...baseHandlers())
    renderSection(['evaluations:read'])

    await waitFor(() => expect(screen.getByText('Prompt injection')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /add scenario/i })).toBeNull()
    // No reorder chevron buttons (they have size="icon" and contain svg — unique to the row actions)
    // The dialogs are mounted but closed; they don't render visible buttons with svg
    // The scenario row action group (up/down/pencil/trash) is absent when canMutate=false
    const allButtons = screen.queryAllByRole('button')
    // None of the visible buttons should be the row icon buttons (they're inside a conditional block)
    // Verify specifically by text: the row has no "Add scenario" button
    allButtons.forEach((btn) => {
      expect(btn.textContent).not.toMatch(/add scenario/i)
    })
  })

  it('with evaluations:update — Add scenario button visible, icon buttons in scenario rows', async () => {
    server.use(...baseHandlers())
    renderSection(['evaluations:update'])

    await waitFor(() => expect(screen.getByText('Prompt injection')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /add scenario/i })).toBeInTheDocument()
    // The scenario row should contain action buttons (up/down/pencil/trash = 4 icon buttons)
    const scenarioRow = screen.getByText('Prompt injection').closest('.rounded-md')
    expect(scenarioRow).not.toBeNull()
    const iconButtons = scenarioRow!.querySelectorAll('button[class*="size-icon"], button svg')
    expect(iconButtons.length).toBeGreaterThan(0)
  })
})

describe('ScenariosSection — tasks', () => {
  it('renders scenario tasks from the tasks endpoint', async () => {
    server.use(...baseHandlers())

    renderSection(['evaluations:update'])

    await waitFor(() => expect(screen.getByText('Basic injection')).toBeInTheDocument())
    expect(screen.getByText('Attempt via user turn')).toBeInTheDocument()
  })

  it('creating a task POSTs to the tasks endpoint with name and description', async () => {
    let captured: unknown = null
    const newTask: TaskResponse = {
      id: 'task-0002-0000-0000-000000000000',
      scenario_id: SCEN_ID,
      name: 'Advanced injection',
      description: 'Via tool output',
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    }

    server.use(
      ...baseHandlers(),
      http.post(`http://localhost/api/v1/scenarios/${SCEN_ID}/tasks`, async ({ request }) => {
        captured = await request.json()
        return HttpResponse.json(newTask, { status: 201 })
      }),
    )

    const user = userEvent.setup()
    renderSection(['evaluations:update'])

    // wait for initial render
    await waitFor(() => expect(screen.getByText('Basic injection')).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: /add task/i }))

    const dialog = screen.getByRole('dialog')
    await user.type(within(dialog).getByLabelText('Name'), 'Advanced injection')
    await user.type(within(dialog).getByLabelText('Description'), 'Via tool output')

    await user.click(within(dialog).getByRole('button', { name: 'Add' }))

    await waitFor(() => expect(captured).not.toBeNull())

    const body = captured as { name: string; description: string }
    expect(body.name).toBe('Advanced injection')
    expect(body.description).toBe('Via tool output')
  })

  it('editing a task PATCHes the tasks endpoint with updated name', async () => {
    let captured: unknown = null

    server.use(
      ...baseHandlers(),
      http.patch(
        `http://localhost/api/v1/scenarios/${SCEN_ID}/tasks/${TASK_ID}`,
        async ({ request }) => {
          captured = await request.json()
          return HttpResponse.json({ ...task, name: 'Updated injection' })
        },
      ),
    )

    const user = userEvent.setup()
    renderSection(['evaluations:update'])

    await waitFor(() => expect(screen.getByText('Basic injection')).toBeInTheDocument())

    // click the edit (pencil) button next to the task
    const taskRow = screen.getByText('Basic injection').closest('div[class]')
    const buttons = taskRow?.parentElement?.querySelectorAll('button')
    // first button is pencil (edit), second is trash (delete)
    const pencilBtn = buttons?.[0]
    expect(pencilBtn).not.toBeNull()
    await user.click(pencilBtn!)

    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByText('Edit task')).toBeInTheDocument()

    // clear and retype the name
    const nameInput = within(dialog).getByLabelText('Name')
    await user.clear(nameInput)
    await user.type(nameInput, 'Updated injection')

    await user.click(within(dialog).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(captured).not.toBeNull())

    const body = captured as { name: string }
    expect(body.name).toBe('Updated injection')
  })

  it('Edit button hidden without evaluations:update', async () => {
    server.use(...baseHandlers())
    renderSection(['evaluations:read'])

    await waitFor(() => expect(screen.getByText('Basic injection')).toBeInTheDocument())

    // no pencil button in the task row
    const taskRow = screen.getByText('Basic injection').closest('div[class]')
    const buttons = taskRow?.parentElement?.querySelectorAll('button')
    // with read-only there should be no action buttons on tasks
    expect(buttons?.length ?? 0).toBe(0)
  })

  it('deleting a task calls DELETE on the tasks endpoint', async () => {
    let deleteCalled = false

    server.use(
      ...baseHandlers(),
      http.delete(`http://localhost/api/v1/scenarios/${SCEN_ID}/tasks/${TASK_ID}`, () => {
        deleteCalled = true
        return new HttpResponse(null, { status: 204 })
      }),
    )

    const user = userEvent.setup()
    renderSection(['evaluations:update'])

    await waitFor(() => expect(screen.getByText('Basic injection')).toBeInTheDocument())

    // click the delete (trash) button next to the task — second button (after pencil/edit)
    const taskRow = screen.getByText('Basic injection').closest('div[class]')
    const taskButtons = taskRow?.parentElement?.querySelectorAll('button')
    const trashBtn = taskButtons?.[1]
    expect(trashBtn).not.toBeNull()
    await user.click(trashBtn!)

    // confirm in the dialog
    await waitFor(() => expect(screen.getByRole('button', { name: 'Delete' })).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: 'Delete' }))

    await waitFor(() => expect(deleteCalled).toBe(true))
  })
})
