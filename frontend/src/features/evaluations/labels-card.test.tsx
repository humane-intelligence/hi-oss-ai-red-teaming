import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { server } from '@/test/msw/server'
import { renderWithProviders } from '@/test/utils'
import { MAX_LABEL_CATEGORIES } from '@/lib/api/limits'
import { LabelsCard } from './labels-card'

const EVAL = 'eval-0001-0000-0000-000000000000'
const url = `http://localhost/api/v1/evaluations/${EVAL}/label-categories`

function category(name: string, values: string[] = []) {
  return {
    id: `id-${name}`,
    evaluation_id: EVAL,
    name,
    values,
    created_at: '2026-01-01T00:00:00Z',
  }
}

describe('LabelsCard', () => {
  it('lists categories with their values, and free-text ones as such', async () => {
    server.use(
      http.get(url, () =>
        HttpResponse.json([category('language', ['english', 'polish']), category('notes')]),
      ),
    )
    renderWithProviders(<LabelsCard evaluationId={EVAL} canManage={false} />)

    expect(await screen.findByText('language')).toBeInTheDocument()
    expect(screen.getByText('english')).toBeInTheDocument()
    expect(screen.getByText('polish')).toBeInTheDocument()
    expect(screen.getByText('Free text')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Custom tags (2)' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Add tag category' })).toBeNull()
    expect(screen.queryByRole('button', { name: /delete language/i })).toBeNull()
  })

  it('tells a manager what labels are when there are none', async () => {
    renderWithProviders(<LabelsCard evaluationId={EVAL} canManage />)

    expect(await screen.findByText(/never sent to the model/i)).toBeInTheDocument()
  })

  it('adds a category with one value per line', async () => {
    let body: unknown = null
    const rows: ReturnType<typeof category>[] = []
    server.use(
      http.get(url, () => HttpResponse.json(rows)),
      http.post(url, async ({ request }) => {
        body = await request.json()
        const b = body as { name: string; values: string[] }
        const row = category(b.name, b.values)
        rows.push(row)
        return HttpResponse.json(row, { status: 201 })
      }),
    )
    const user = userEvent.setup()
    renderWithProviders(<LabelsCard evaluationId={EVAL} canManage />)

    await user.click(await screen.findByRole('button', { name: 'Add tag category' }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByLabelText('Tag category'), 'language')
    await user.type(within(dialog).getByLabelText('Tags'), 'english{Enter}  {Enter}polish ')
    await user.click(within(dialog).getByRole('button', { name: /^add$/i }))

    await waitFor(() => expect(body).toEqual({ name: 'language', values: ['english', 'polish'] }))
    expect(await screen.findByText('polish')).toBeInTheDocument()
  })

  it('refuses a value listed twice before sending', async () => {
    let posted = false
    server.use(
      http.post(url, () => {
        posted = true
        return HttpResponse.json({}, { status: 201 })
      }),
    )
    const user = userEvent.setup()
    renderWithProviders(<LabelsCard evaluationId={EVAL} canManage />)

    await user.click(await screen.findByRole('button', { name: 'Add tag category' }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByLabelText('Tag category'), 'language')
    await user.type(within(dialog).getByLabelText('Tags'), 'Polish{Enter}polish')
    await user.click(within(dialog).getByRole('button', { name: /^add$/i }))

    expect(await within(dialog).findByText(/listed twice/i)).toBeInTheDocument()
    expect(posted).toBe(false)
  })

  it('keeps the dialog and its input when the name is already taken', async () => {
    server.use(
      http.post(url, () =>
        HttpResponse.json(
          {
            title: 'Conflict',
            status: 409,
            detail: "Label category 'language' already exists on this evaluation.",
          },
          { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const user = userEvent.setup()
    renderWithProviders(<LabelsCard evaluationId={EVAL} canManage />)

    await user.click(await screen.findByRole('button', { name: 'Add tag category' }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByLabelText('Tag category'), 'language')
    await user.click(within(dialog).getByRole('button', { name: /^add$/i }))

    await waitFor(() =>
      expect(within(dialog).getByRole('button', { name: /^add$/i })).toBeEnabled(),
    )
    expect(dialog).toBeVisible()
    expect(within(dialog).getByLabelText('Tag category')).toHaveValue('language')
  })

  it('edits a category with its current values prefilled', async () => {
    let body: unknown = null
    server.use(
      http.get(url, () => HttpResponse.json([category('language', ['english'])])),
      http.patch(`${url}/id-language`, async ({ request }) => {
        body = await request.json()
        return HttpResponse.json(category('language', ['english', 'polish']))
      }),
    )
    const user = userEvent.setup()
    renderWithProviders(<LabelsCard evaluationId={EVAL} canManage />)

    await user.click(await screen.findByRole('button', { name: 'Edit language' }))
    const dialog = await screen.findByRole('dialog')
    const values = within(dialog).getByLabelText('Tags')
    expect(values).toHaveValue('english')
    await user.type(values, '{Enter}polish')
    await user.click(within(dialog).getByRole('button', { name: /^save$/i }))

    await waitFor(() => expect(body).toEqual({ values: ['english', 'polish'] }))
  })

  it('sends a rename without the values, so it cannot overwrite a list changed elsewhere', async () => {
    let body: unknown = null
    server.use(
      http.get(url, () => HttpResponse.json([category('language', ['english'])])),
      http.patch(`${url}/id-language`, async ({ request }) => {
        body = await request.json()
        return HttpResponse.json(category('lang', ['english']))
      }),
    )
    const user = userEvent.setup()
    renderWithProviders(<LabelsCard evaluationId={EVAL} canManage />)

    await user.click(await screen.findByRole('button', { name: 'Edit language' }))
    const dialog = await screen.findByRole('dialog')
    const name = within(dialog).getByLabelText('Tag category')
    await user.clear(name)
    await user.type(name, 'lang')
    await user.click(within(dialog).getByRole('button', { name: /^save$/i }))

    await waitFor(() => expect(body).toEqual({ name: 'lang' }))
  })

  it('does not send values that only gained a blank line', async () => {
    let body: unknown = null
    server.use(
      http.get(url, () => HttpResponse.json([category('language', ['english'])])),
      http.patch(`${url}/id-language`, async ({ request }) => {
        body = await request.json()
        return HttpResponse.json(category('lang', ['english']))
      }),
    )
    const user = userEvent.setup()
    renderWithProviders(<LabelsCard evaluationId={EVAL} canManage />)

    await user.click(await screen.findByRole('button', { name: 'Edit language' }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByLabelText('Tags'), '{Enter}')
    const name = within(dialog).getByLabelText('Tag category')
    await user.clear(name)
    await user.type(name, 'lang')
    await user.click(within(dialog).getByRole('button', { name: /^save$/i }))

    await waitFor(() => expect(body).toEqual({ name: 'lang' }))
  })

  it('closes without a request when nothing was edited', async () => {
    let patched = false
    server.use(
      http.get(url, () => HttpResponse.json([category('language', ['english'])])),
      http.patch(`${url}/id-language`, () => {
        patched = true
        return HttpResponse.json(category('language', ['english']))
      }),
    )
    const user = userEvent.setup()
    renderWithProviders(<LabelsCard evaluationId={EVAL} canManage />)

    await user.click(await screen.findByRole('button', { name: 'Edit language' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: /^save$/i }))

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(patched).toBe(false)
  })

  it('deletes a category only after the delete is confirmed', async () => {
    let deleted = false
    server.use(
      http.get(url, () => HttpResponse.json(deleted ? [] : [category('language')])),
      http.delete(`${url}/id-language`, () => {
        deleted = true
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const user = userEvent.setup()
    renderWithProviders(<LabelsCard evaluationId={EVAL} canManage />)

    await user.click(await screen.findByRole('button', { name: 'Delete language' }))
    expect(deleted).toBe(false)
    const confirm = await screen.findByRole('dialog')
    expect(within(confirm).getByText(/can't be restored/i)).toBeInTheDocument()
    await user.click(within(confirm).getByRole('button', { name: /^delete$/i }))

    await waitFor(() => expect(deleted).toBe(true))
    await waitFor(() => expect(screen.queryByText('language')).toBeNull())
    expect(screen.getByRole('button', { name: 'Add tag category' })).toHaveFocus()
  })

  it('disables Add at the category cap', async () => {
    server.use(
      http.get(url, () =>
        HttpResponse.json(
          Array.from({ length: MAX_LABEL_CATEGORIES }, (_, i) => category(`c${i}`)),
        ),
      ),
    )
    renderWithProviders(<LabelsCard evaluationId={EVAL} canManage />)

    expect(await screen.findByRole('button', { name: 'Add tag category' })).toBeDisabled()
    expect(screen.getByText(/up to 20 tag categories/i)).toBeInTheDocument()
  })

  it('returns focus to Add after a delete at the cap, once Add is enabled again', async () => {
    let rows = Array.from({ length: MAX_LABEL_CATEGORIES }, (_, i) => category(`c${i}`))
    server.use(
      http.get(url, () => HttpResponse.json(rows)),
      http.delete(`${url}/id-c0`, () => {
        rows = rows.slice(1)
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const user = userEvent.setup()
    renderWithProviders(<LabelsCard evaluationId={EVAL} canManage />)

    await user.click(await screen.findByRole('button', { name: 'Delete c0' }))
    await user.click(
      within(await screen.findByRole('dialog')).getByRole('button', { name: /^delete$/i }),
    )

    const add = screen.getByRole('button', { name: 'Add tag category' })
    await waitFor(() => expect(add).toHaveFocus())
    expect(add).toBeEnabled()
  })

  it('offers a retry when the categories fail to load', async () => {
    let fail = true
    server.use(
      http.get(url, () =>
        fail
          ? HttpResponse.json({ title: 'Boom', status: 500 }, { status: 500 })
          : HttpResponse.json([category('language')]),
      ),
    )
    const user = userEvent.setup()
    renderWithProviders(<LabelsCard evaluationId={EVAL} canManage />)

    const retry = await screen.findByRole('button', { name: 'Retry' })
    fail = false
    await user.click(retry)
    expect(await screen.findByText('language')).toBeInTheDocument()
  })

  it('reports a rejected value the form has no field for', async () => {
    server.use(
      http.post(url, () =>
        HttpResponse.json(
          {
            title: 'Unprocessable Entity',
            status: 422,
            detail: 'Validation failed',
            errors: [
              {
                loc: ['body', 'values', 1],
                msg: 'Value error, must not be blank',
                type: 'value_error',
              },
            ],
          },
          { status: 422, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const user = userEvent.setup()
    renderWithProviders(<LabelsCard evaluationId={EVAL} canManage />)

    await user.click(await screen.findByRole('button', { name: 'Add tag category' }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByLabelText('Tag category'), 'language')
    await user.type(within(dialog).getByLabelText('Tags'), 'english{Enter}polish')
    await user.click(within(dialog).getByRole('button', { name: /^add$/i }))

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'Tag 2: Value error, must not be blank',
    )
  })

  it('drops invisible characters, so a line of only those is no value', async () => {
    let body: unknown = null
    server.use(
      http.post(url, async ({ request }) => {
        body = await request.json()
        return HttpResponse.json(category('language', ['english']), { status: 201 })
      }),
    )
    const user = userEvent.setup()
    renderWithProviders(<LabelsCard evaluationId={EVAL} canManage />)

    await user.click(await screen.findByRole('button', { name: 'Add tag category' }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByLabelText('Tag category'), 'lang\u200buage')
    await user.type(within(dialog).getByLabelText('Tags'), 'english{Enter}\u200b')
    await user.click(within(dialog).getByRole('button', { name: /^add$/i }))

    await waitFor(() => expect(body).toEqual({ name: 'language', values: ['english'] }))
  })

  it('keeps the last loaded list, with a retry, when a refresh fails', async () => {
    let fail = false
    server.use(
      http.get(url, () =>
        fail
          ? HttpResponse.json({ title: 'Boom', status: 500 }, { status: 500 })
          : HttpResponse.json([category('language')]),
      ),
      http.delete(`${url}/id-language`, () => {
        fail = true
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const user = userEvent.setup()
    renderWithProviders(<LabelsCard evaluationId={EVAL} canManage />)

    await user.click(await screen.findByRole('button', { name: 'Delete language' }))
    await user.click(
      within(await screen.findByRole('dialog')).getByRole('button', { name: /^delete$/i }),
    )

    expect(await screen.findByText(/couldn't refresh the custom tags/i)).toBeInTheDocument()
    expect(screen.getByText('language')).toBeInTheDocument()
    fail = false
    server.use(http.get(url, () => HttpResponse.json([])))
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(screen.queryByText('language')).toBeNull())
  })
})
