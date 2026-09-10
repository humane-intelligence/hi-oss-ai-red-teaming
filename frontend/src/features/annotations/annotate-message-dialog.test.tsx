import { beforeEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { toast } from 'sonner'
import { server } from '@/test/msw/server'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { AnnotateMessageDialog } from './annotate-message-dialog'
import type { AnnotationResponse } from '@/lib/api/types'

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

const LABELS_URL = 'http://localhost/api/v1/annotation-labels'
const URL = 'http://localhost/api/v1/annotations'
const MSG_ID = 'msg-0001'
const JAILBREAK = { id: 'label-jb', key: 'jailbreak', name: 'Jailbreak', is_custom: false }

// `authWrapper`'s caller is id '1'.
function annotation(
  id: string,
  label: { id: string; key: string | null; name: string; is_custom: boolean },
  by = '1',
) {
  return {
    id,
    message_id: MSG_ID,
    conversation_id: 'conv-1',
    label,
    created_by_id: by,
    evaluation_id: 'eval-1',
    evaluation_group_id: 'group-1',
    created_at: '2026-08-27T12:00:00Z',
    updated_at: '2026-08-27T12:00:00Z',
  } as AnnotationResponse
}

const posted: unknown[] = []
const deleted: string[] = []

beforeEach(() => {
  posted.length = 0
  deleted.length = 0
  server.use(
    http.get(LABELS_URL, () =>
      HttpResponse.json({ items: [JAILBREAK], total: 1, limit: 100, offset: 0 }),
    ),
    http.post(URL, async ({ request }) => {
      const body = await request.json()
      posted.push(body)
      return HttpResponse.json(annotation('new', JAILBREAK), { status: 201 })
    }),
    http.delete(`${URL}/:id`, ({ params }) => {
      deleted.push(String(params.id))
      return new HttpResponse(null, { status: 204 })
    }),
  )
})

function renderDialog(annotations: AnnotationResponse[] = []) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = authWrapper(['annotations:read', 'annotations:create', 'annotations:delete'])
  return render(
    <QueryClientProvider client={qc}>
      <Wrapper>
        <AnnotateMessageDialog
          messageId={MSG_ID}
          conversationId="conv-1"
          annotations={annotations}
          currentUserId="1"
          open
          onOpenChange={() => {}}
        />
      </Wrapper>
    </QueryClientProvider>,
  )
}

describe('AnnotateMessageDialog', () => {
  it('sends a catalog pick as label_id', async () => {
    const user = userEvent.setup()
    renderDialog()

    await user.click(screen.getByLabelText('Labels'))
    await user.click(await screen.findByRole('option', { name: /jailbreak/i }))

    await waitFor(() => expect(posted).toEqual([{ message_id: MSG_ID, label_id: 'label-jb' }]))
  })

  it('sends a typed label as text, never as an id', async () => {
    // The whole point of the two-branch payload: a typed label must not be mistaken for a
    // catalog reference, or the server would 404 on an unknown label id.
    const user = userEvent.setup()
    renderDialog()

    await user.type(screen.getByLabelText('Labels'), 'prompt injection')
    await user.click(await screen.findByRole('option', { name: /prompt injection/i }))

    await waitFor(() => expect(posted).toEqual([{ message_id: MSG_ID, text: 'prompt injection' }]))
  })

  it('removes the caller’s own label by deleting that annotation', async () => {
    const user = userEvent.setup()
    renderDialog([annotation('mine', JAILBREAK)])

    await user.click(screen.getByRole('button', { name: /remove jailbreak/i }))

    await waitFor(() => expect(deleted).toEqual(['mine']))
    expect(posted).toEqual([])
  })

  it('shows another annotator’s label without offering to remove it', async () => {
    // Shared reads mean a colleague's labels are visible here; deleting one is refused with a
    // 403, so the dialog must not offer the control at all.
    renderDialog([
      annotation(
        'theirs',
        { id: 'l-x', key: null, name: 'someone else', is_custom: true },
        'user-0002',
      ),
    ])

    expect(screen.getByText('someone else')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /remove someone else/i })).not.toBeInTheDocument()
  })

  it('says typing a shared label’s name reuses that label', async () => {
    // The create-a-value affordance is not discoverable from the combobox role, and
    // curated-first resolution is a rule the operator cannot infer.
    renderDialog()

    expect(screen.getByText(/typing the name of a shared label uses that one/i)).toBeInTheDocument()
    // Only the hint below the cap — the truncation caveat is not rendered, so its id must not
    // dangle in the description.
    const describedBy = screen.getByLabelText('Labels').getAttribute('aria-describedby') ?? ''
    expect(describedBy.split(' ')).toHaveLength(1)
  })

  it('says so when the vocabulary is longer than the one page it fetches', async () => {
    // Past the cap the picker silently stops suggesting, and its search filters only the page
    // it holds — so a label that exists is neither listed nor findable.
    server.use(
      http.get(LABELS_URL, () =>
        HttpResponse.json({ items: [JAILBREAK], total: 250, limit: 100, offset: 0 }),
      ),
    )
    renderDialog()

    const caveat = await screen.findByText(/showing 100 of 250 labels/i)
    // And in the field's accessible description, not only on screen: it is a caveat about what
    // the search will fail to find, so it has to reach the operator before they type, not after.
    const describedBy = screen.getByLabelText('Labels').getAttribute('aria-describedby') ?? ''
    expect(describedBy.split(' ')).toContain(caveat.id)
  })

  it('clears a search typed on one message before reopening on another', async () => {
    // One dialog serves the whole page, so `term` outlives the message it was typed on. The
    // picker's mount debounce (`onSearchChange('')`, 300ms) does clear it eventually, so the
    // window is transient rather than permanent — which is exactly why this asserts
    // synchronously on reopen, before that timer fires.
    const user = userEvent.setup()
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const Wrapper = authWrapper(['annotations:read', 'annotations:create', 'annotations:delete'])
    const view = (open: boolean) => (
      <QueryClientProvider client={qc}>
        <Wrapper>
          <AnnotateMessageDialog
            messageId={MSG_ID}
            conversationId="conv-1"
            annotations={[]}
            currentUserId="1"
            open={open}
            onOpenChange={() => {}}
          />
        </Wrapper>
      </QueryClientProvider>
    )
    const { rerender } = render(view(true))

    await user.type(screen.getByLabelText('Labels'), 'prompt injection')
    await waitFor(() =>
      expect(screen.queryByRole('option', { name: /^jailbreak$/i })).not.toBeInTheDocument(),
    )

    rerender(view(false))
    rerender(view(true))

    // `fireEvent` and a synchronous assertion: no awaits, so the remount's debounce has not run
    // and what is asserted is the state the operator would actually see on reopening.
    fireEvent.focus(screen.getByLabelText('Labels'))
    expect(screen.getByRole('option', { name: /jailbreak/i })).toBeInTheDocument()
  })

  it('reports a vocabulary that could not be loaded', async () => {
    server.use(http.get(LABELS_URL, () => new HttpResponse(null, { status: 500 })))
    renderDialog()

    expect(await screen.findByText(/could not load the label vocabulary/i)).toBeInTheDocument()
  })
})

describe('AnnotateMessageDialog — a retired label', () => {
  const RETIRED = { id: 'label-gone', key: 'retired', name: 'Retired Label', is_custom: false }

  it('names it on the chip and still removes it as a catalog label', async () => {
    // A retired label leaves the vocabulary endpoint but stays embedded in the annotation.
    // Without folding it into the options the chip would render the raw uuid, and the payload
    // would be mistaken for typed text.
    const user = userEvent.setup()
    renderDialog([annotation('mine', RETIRED)])

    expect(await screen.findByText('Retired Label')).toBeInTheDocument()
    expect(screen.queryByText(RETIRED.id)).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /remove retired label/i }))

    await waitFor(() => expect(deleted).toEqual(['mine']))
  })
})

describe('AnnotateMessageDialog — an over-long typed label', () => {
  it('refuses it client-side rather than letting the server 422', async () => {
    const user = userEvent.setup()
    renderDialog()
    const tooLong = 'x'.repeat(129)

    await user.type(screen.getByLabelText('Labels'), tooLong)
    await user.click(await screen.findByRole('option', { name: /create/i }))

    await waitFor(() => expect(toast.error).toHaveBeenCalled())
    expect(posted).toEqual([])
  })
})

describe('AnnotateMessageDialog — a previously typed label', () => {
  const MINE = { id: 'label-mine', key: null, name: 'roleplay bypass', is_custom: true }

  it('is offered as a suggestion and picked by id, not retyped', async () => {
    // The point of storing typed labels as per-author rows: the second message offers the
    // label back instead of making the annotator retype it, and picking it sends `label_id`.
    server.use(
      http.get(LABELS_URL, () =>
        HttpResponse.json({ items: [JAILBREAK, MINE], total: 2, limit: 100, offset: 0 }),
      ),
    )
    const user = userEvent.setup()
    renderDialog()

    await user.type(screen.getByLabelText('Labels'), 'roleplay')
    await user.click(await screen.findByRole('option', { name: /roleplay bypass/i }))

    await waitFor(() => expect(posted).toEqual([{ message_id: MSG_ID, label_id: 'label-mine' }]))
  })

  it('suppresses the create row when a suggestion already matches', async () => {
    // Typing the exact name of a label already offered must reuse it rather than asking the
    // server to create a duplicate under a second id.
    server.use(
      http.get(LABELS_URL, () =>
        HttpResponse.json({ items: [MINE], total: 1, limit: 100, offset: 0 }),
      ),
    )
    const user = userEvent.setup()
    renderDialog()

    await user.type(screen.getByLabelText('Labels'), 'roleplay bypass')

    expect(await screen.findByRole('option', { name: /roleplay bypass/i })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /^create/i })).not.toBeInTheDocument()
  })
})
