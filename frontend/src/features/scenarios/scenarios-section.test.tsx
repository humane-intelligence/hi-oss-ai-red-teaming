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
  it('with evaluations:read only — no scenario or task controls', async () => {
    server.use(...baseHandlers())
    renderSection(['evaluations:read'])

    expect(await screen.findByText('Basic injection')).toBeInTheDocument()
    for (const name of [
      /add scenario/i,
      /add task/i,
      /move scenario/i,
      /edit scenario/i,
      /delete scenario/i,
      /edit task/i,
      /delete task/i,
    ]) {
      expect(screen.queryByRole('button', { name })).toBeNull()
    }
  })

  it('with evaluations:update — Add scenario button visible, icon buttons in scenario rows', async () => {
    server.use(...baseHandlers())
    renderSection(['evaluations:update'])

    await waitFor(() => expect(screen.getByText('Prompt injection')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /add scenario/i })).toBeInTheDocument()
    expect(await screen.findByText('Basic injection')).toBeInTheDocument()
    for (const name of [
      'Move scenario 1: Prompt injection up',
      'Move scenario 1: Prompt injection down',
      'Edit scenario 1: Prompt injection',
      'Delete scenario 1: Prompt injection',
      'Edit task 1: Basic injection',
      'Delete task 1: Basic injection',
      'Add task to scenario 1: Prompt injection',
    ]) {
      expect(screen.getByRole('button', { name })).toBeInTheDocument()
    }
  })
})

describe('ScenariosSection — cards', () => {
  it('numbers scenarios and tasks and labels the scenario details', async () => {
    server.use(...baseHandlers())
    renderSection(['evaluations:update'])

    expect(await screen.findByText('Basic injection')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /Evaluation content/ })).toBeInTheDocument()
    expect(
      screen.getByRole('heading', { level: 3, name: 'Scenario 1: Prompt injection' }),
    ).toBeInTheDocument()
    expect(screen.getByText('Task 1:')).toBeInTheDocument()
    expect(screen.getByText('Date added')).toBeInTheDocument()
    expect(screen.getByText('Required reviewers')).toBeInTheDocument()
    expect(screen.getByText('Scenario description')).toBeInTheDocument()
  })

  it('keeps the reviewer count from those who cannot change the scenario', async () => {
    server.use(...baseHandlers())
    renderSection([])

    expect(await screen.findByText('Prompt injection')).toBeInTheDocument()
    expect(screen.queryByText('Required reviewers')).toBeNull()
    expect(screen.queryByRole('button', { name: /add task/i })).toBeNull()
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

    await user.click(screen.getByRole('button', { name: 'Edit task 1: Basic injection' }))

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

  it('shows the error instead of an empty task list when tasks fail to load', async () => {
    server.use(
      http.get(`http://localhost/api/v1/scenarios/${SCEN_ID}/tasks`, () =>
        HttpResponse.json({ title: 'Server error', status: 500 }, { status: 500 }),
      ),
      ...baseHandlers(),
    )
    renderSection(['evaluations:update'])

    expect(await screen.findByText(/something went wrong on the server/i)).toBeInTheDocument()
    expect(screen.queryByText(/no tasks yet/i)).toBeNull()
    expect(screen.queryByText(/Tasks \(0\)/)).toBeNull()
  })

  it('renders no task list for a scenario without tasks', async () => {
    server.use(
      http.get(`http://localhost/api/v1/scenarios/${SCEN_ID}/tasks`, () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
      ...baseHandlers(),
    )
    renderSection(['evaluations:update'])

    expect(await screen.findByText(/no tasks yet\. add tasks/i)).toBeInTheDocument()
    expect(screen.getAllByRole('list')).toHaveLength(1)
  })

  it('renders no scenario list for an evaluation without scenarios', async () => {
    server.use(
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/scenarios`, () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
    )
    renderSection(['evaluations:update'])

    expect(await screen.findByText(/no scenarios yet\. add a scenario/i)).toBeInTheDocument()
    expect(screen.queryByRole('list')).toBeNull()
  })

  it('offers no call to add tasks to those who cannot', async () => {
    server.use(
      http.get(`http://localhost/api/v1/scenarios/${SCEN_ID}/tasks`, () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
      ...baseHandlers(),
    )
    renderSection(['evaluations:read'])

    expect(await screen.findByText(/^No tasks yet\.$/)).toBeInTheDocument()
  })

  it('offers no call to add a scenario to those who cannot', async () => {
    server.use(
      http.get(`http://localhost/api/v1/evaluations/${EVAL_ID}/scenarios`, () =>
        HttpResponse.json({ items: [], total: 0, limit: 100, offset: 0 }),
      ),
    )
    renderSection(['evaluations:read'])

    expect(await screen.findByText(/^No scenarios yet\.$/)).toBeInTheDocument()
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

    await user.click(screen.getByRole('button', { name: 'Delete task 1: Basic injection' }))

    // confirm in the dialog
    await waitFor(() => expect(screen.getByRole('button', { name: 'Delete' })).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: 'Delete' }))

    await waitFor(() => expect(deleteCalled).toBe(true))
  })
})
