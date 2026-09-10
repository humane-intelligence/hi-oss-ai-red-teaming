import { afterEach, describe, expect, it, vi } from 'vitest'
import { StrictMode, type ReactNode } from 'react'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { toast } from 'sonner'
import { server } from '@/test/msw/server'
import { MAX_TAGS } from '@/lib/api/limits'
import { queryClient } from '@/lib/query'
import { EditTagsDialog } from './edit-tags-dialog'
import type { AllowedKeysStatus } from './tag-key-field'

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

const PATCH_URL = 'http://localhost/api/v1/evaluations/eval-1/conversations/conv-1'

type DialogOpts = {
  allowedKeys?: string[] | null
  keysStatus?: AllowedKeysStatus
  onOpenChange?: (open: boolean) => void
  open?: boolean
}

function dialog(tags: Record<string, string>, opts: DialogOpts = {}) {
  return (
    <EditTagsDialog
      evaluationId="eval-1"
      conversationId="conv-1"
      currentTags={tags}
      allowedKeys={opts.allowedKeys ?? null}
      keysStatus={opts.keysStatus ?? 'success'}
      open={opts.open ?? true}
      onOpenChange={opts.onOpenChange ?? (() => {})}
    />
  )
}

// A `wrapper` (not inline providers) so `rerender` keeps the QueryClient — the point of these
// tests is a prop change on a mounted dialog, which is what a background refetch looks like.
function renderDialog(tags: Record<string, string>, opts: DialogOpts = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  return render(dialog(tags, opts), { wrapper: Wrapper })
}

// jsdom implements no <dialog> behaviour, so what is asserted is what the browser acts on: Esc closes
// a modal dialog through the key event, and the guard has to prevent that. (Preventing the `cancel`
// event instead does not hold in Chrome — measured — which is why this fires a keydown.)
function pressEsc() {
  const event = new KeyboardEvent('keydown', { key: 'Escape', cancelable: true, bubbles: true })
  screen.getByRole('dialog').dispatchEvent(event)
  return event
}

async function editValueAndSave(user: ReturnType<typeof userEvent.setup>, value: string) {
  await user.clear(screen.getByLabelText('Tag 1 value'))
  await user.type(screen.getByLabelText('Tag 1 value'), value)
  await user.click(screen.getByRole('button', { name: /^save$/i }))
}

afterEach(() => vi.clearAllMocks())

describe('EditTagsDialog — server refetch while open', () => {
  it('picks up a tag added elsewhere as long as the user has not edited', async () => {
    // The save writes the whole map, so rows pinned at open time would drop the new tag.
    const { rerender } = renderDialog({ env: 'prod' })
    expect(screen.getByLabelText('Tag 1 key')).toHaveValue('env')

    rerender(dialog({ env: 'prod', owner: 'ana' }))

    expect(screen.getByLabelText('Tag 2 key')).toHaveValue('owner')
    expect(screen.getByLabelText('Tag 2 value')).toHaveValue('ana')
  })

  it('keeps the in-progress draft when the refetch lands after an edit', async () => {
    const user = userEvent.setup()
    const { rerender } = renderDialog({ env: 'prod' })
    await user.clear(screen.getByLabelText('Tag 1 value'))
    await user.type(screen.getByLabelText('Tag 1 value'), 'staging')

    rerender(dialog({ env: 'prod', owner: 'ana' }))

    expect(screen.getByLabelText('Tag 1 value')).toHaveValue('staging')
    expect(screen.queryByLabelText('Tag 2 key')).toBeNull()
  })
})

describe('EditTagsDialog — the allow-list query is not settled', () => {
  it.each([
    ['pending', 'Loading allowed keys…'],
    ['error', "Couldn't load the allowed keys — reload the page to try again."],
  ] as const)(
    'says the allow-list is %s instead of showing an empty picker',
    async (status, copy) => {
      // Gating the "allows no keys yet" line on a settled query fixed the false claim, but left the
      // pending and failed states rendering nothing — a select holding only its placeholder, with no
      // way to tell "loading" from "failed" from "none allowed". Pending is the common case: it is
      // every first open on a restricted evaluation.
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
      render(
        <QueryClientProvider client={qc}>
          <EditTagsDialog
            evaluationId="eval-1"
            conversationId="conv-1"
            currentTags={{}}
            allowedKeys={[]}
            keysStatus={status}
            open
            onOpenChange={() => {}}
          />
        </QueryClientProvider>,
      )

      expect(await screen.findByText(copy)).toBeInTheDocument()
      expect(screen.queryByText(/No keys are allowed yet/)).toBeNull()
    },
  )

  it.each(['pending', 'error'] as const)(
    'refuses a new row while the list is %s, pointing at the line that says why',
    (keysStatus) => {
      // `[].every()` is vacuously true, so an unsettled list reads as "every allowed key is already
      // used" — a false reason, in the one place a disabled button cannot show it. A new row is still
      // refused (its picker would have nothing in it), and the button points at the loading/error line
      // rather than repeating it.
      renderDialog({}, { allowedKeys: [], keysStatus })

      const addTag = screen.getByRole('button', { name: /^add tag$/i })
      expect(addTag).toHaveAttribute('aria-disabled', 'true')
      expect(screen.queryByText(/already used/i)).toBeNull()
      const reasonId = addTag.getAttribute('aria-describedby')
      expect(document.getElementById(reasonId as string)).toHaveTextContent(/see the note above/i)
    },
  )
})

describe('EditTagsDialog — the counter and a blocked "Add tag"', () => {
  it('offers Save and keeps a stored valueless tag when another row is edited', async () => {
    // The dialog is what passes the conversation's stored map into `tagsFromRows`; without it a legacy
    // valueless tag makes every unrelated edit unsavable. The helper's own tests can't see that — only
    // the call site can, so this is the test that pins the second argument.
    let captured: unknown = null
    server.use(
      http.patch(PATCH_URL, async ({ request }) => {
        captured = await request.json()
        return HttpResponse.json({})
      }),
    )
    const user = userEvent.setup()
    renderDialog({ env: 'prod', persona: '' })

    await user.clear(screen.getByLabelText('Tag 1 value'))
    await user.type(screen.getByLabelText('Tag 1 value'), 'staging')
    await user.click(screen.getByRole('button', { name: /^save$/i }))

    await waitFor(() => expect(captured).not.toBeNull())
    expect((captured as { tags: Record<string, string> }).tags).toEqual({
      env: 'staging',
      persona: '',
    })
  })

  it('counts tags, not rows, so an untouched empty dialog reads zero', () => {
    // The seeded blank row is neither stored nor sent; counting it reported "1/16 tags" over an
    // empty map, and at the cap it blocked a legal tag.
    renderDialog({})

    expect(screen.getByText(`0/${MAX_TAGS} tags`)).toBeInTheDocument()
  })

  it('refuses a second blank row and says which row to fill', async () => {
    // The cap counts tags, so nothing else bounds the row list: without this the button appends blank
    // rows forever while the counter reads 0/16. The composer enforces the same rule.
    const user = userEvent.setup()
    renderDialog({ env: 'prod' })

    await user.click(screen.getByRole('button', { name: /^add tag$/i }))

    const addTag = screen.getByRole('button', { name: /^add tag$/i })
    expect(addTag).toHaveAttribute('aria-disabled', 'true')
    expect(
      document.getElementById(addTag.getAttribute('aria-describedby') as string),
    ).toHaveTextContent(/fill the empty row first/i)
  })

  it('states the reason beside a refused "Add tag", and wires it as the description', () => {
    // `Button` carries `disabled:pointer-events-none`, so a `title` on a disabled control never gets
    // the hover that would render it — the reason has to be on the page. And the control keeps
    // `aria-disabled` rather than `disabled`, so a keyboard user can reach it and hear that reason.
    renderDialog({ env: 'prod' }, { allowedKeys: ['env'] })

    const addTag = screen.getByRole('button', { name: /^add tag$/i })
    expect(addTag).toHaveAttribute('aria-disabled', 'true')
    const reasonId = addTag.getAttribute('aria-describedby')
    expect(document.getElementById(reasonId as string)).toHaveTextContent(
      /every key this evaluation allows is already used/i,
    )
  })
})

describe('EditTagsDialog — a write in flight', () => {
  it('holds the dialog open on Esc and reports the failure inline', async () => {
    // Cancel is disabled while saving for a reason; Esc bypasses it, and the mutation opts out of the
    // global toast, so a dialog that closes here takes the only report of a lost write with it.
    let release: () => void = () => {}
    server.use(
      http.patch(PATCH_URL, async () => {
        await new Promise<void>((resolve) => {
          release = resolve
        })
        return HttpResponse.json({ detail: 'boom' }, { status: 500 })
      }),
    )
    const user = userEvent.setup()
    renderDialog({ env: 'prod' })
    await editValueAndSave(user, 'staging')
    await waitFor(() => expect(screen.getByRole('button', { name: /saving/i })).toBeDisabled())

    expect(screen.getByRole('button', { name: /^cancel$/i })).toBeDisabled()
    expect(pressEsc().defaultPrevented).toBe(true)

    release()
    expect(await screen.findByText(/something went wrong on the server/i)).toBeInTheDocument()
  })

  it('lets Esc close the dialog when no write is in flight', () => {
    renderDialog({ env: 'prod' })

    expect(pressEsc().defaultPrevented).toBe(false)
  })

  it('falls back to a toast when the dialog is gone before the failure lands', async () => {
    // A route change unmounts the dialog mid-write. An inline error on a component that no longer
    // exists reports the lost write to nobody. Rendered under StrictMode because that is what the app
    // does: a "mounted" flag cleared only by an unmount cleanup is already false on the first commit.
    let release: () => void = () => {}
    server.use(
      http.patch(PATCH_URL, async () => {
        await new Promise<void>((resolve) => {
          release = resolve
        })
        return HttpResponse.json({ detail: 'boom' }, { status: 500 })
      }),
    )
    const user = userEvent.setup()
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { unmount } = render(
      <StrictMode>
        <QueryClientProvider client={qc}>{dialog({ env: 'prod' })}</QueryClientProvider>
      </StrictMode>,
    )
    await editValueAndSave(user, 'staging')
    await waitFor(() => expect(screen.getByRole('button', { name: /saving/i })).toBeDisabled())
    // Still open here, so the report belongs inline — not on a toast.
    expect(toast.error).not.toHaveBeenCalled()

    unmount()
    release()

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        expect.stringMatching(/something went wrong on the server/i),
      ),
    )
  })

  it('reports inline, not on a toast, while the dialog is still open', async () => {
    // The mirror of the case above, and the one StrictMode used to break: a dialog that is still on
    // screen must show its own failure, or the operator gets a toast for a form they are looking at.
    let release: () => void = () => {}
    server.use(
      http.patch(PATCH_URL, async () => {
        await new Promise<void>((resolve) => {
          release = resolve
        })
        return HttpResponse.json({ detail: 'boom' }, { status: 500 })
      }),
    )
    const user = userEvent.setup()
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <StrictMode>
        <QueryClientProvider client={qc}>{dialog({ env: 'prod' })}</QueryClientProvider>
      </StrictMode>,
    )
    await editValueAndSave(user, 'staging')
    await waitFor(() => expect(screen.getByRole('button', { name: /saving/i })).toBeDisabled())

    release()

    // `role="alert"`: this dialog is the mutation's only channel, so the message has to be announced —
    // asserting the text alone would pass with the role removed, i.e. with a silent failure.
    expect(await screen.findByRole('alert')).toHaveTextContent(
      /something went wrong on the server/i,
    )
    expect(toast.error).not.toHaveBeenCalled()
  })

  it('falls back to a toast when the parent closes the dialog mid-write', async () => {
    // Esc is held and Cancel is disabled, but the parent still owns `open` — closing it that way
    // unmounts the dialog's body, so its inline error would render into nothing.
    let release: () => void = () => {}
    server.use(
      http.patch(PATCH_URL, async () => {
        await new Promise<void>((resolve) => {
          release = resolve
        })
        return HttpResponse.json({ detail: 'boom' }, { status: 500 })
      }),
    )
    const user = userEvent.setup()
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const Wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    )
    const { rerender } = render(dialog({ env: 'prod' }), { wrapper: Wrapper })
    await editValueAndSave(user, 'staging')
    await waitFor(() => expect(screen.getByRole('button', { name: /saving/i })).toBeDisabled())

    rerender(dialog({ env: 'prod' }, { open: false }))
    release()

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        expect.stringMatching(/something went wrong on the server/i),
      ),
    )
  })
})

describe('EditTagsDialog — what a failure says', () => {
  it('clears a rejected-key message as soon as the row is edited', async () => {
    // The message names the keys that were submitted, so an edit makes it stale — it would still
    // report a key the operator has already replaced.
    server.use(
      http.patch(PATCH_URL, () =>
        HttpResponse.json(
          { detail: 'Tag key(s) not allowed for this evaluation: legacy.' },
          { status: 400 },
        ),
      ),
    )
    const user = userEvent.setup()
    renderDialog({ legacy: 'x' })
    await editValueAndSave(user, 'y')
    expect(await screen.findByText(/not allowed for this evaluation/i)).toBeInTheDocument()

    await user.type(screen.getByLabelText('Tag 1 value'), 'z')

    expect(screen.queryByText(/not allowed for this evaluation/i)).toBeNull()
  })

  it('keeps the curated copy for a permission failure rather than the raw detail', async () => {
    // `humanizeError` returns `problem.detail` for client errors below 500 — except 403/404, whose
    // detail carries permission slugs. Reading `detail` first would have leaked exactly those.
    server.use(
      http.patch(PATCH_URL, () =>
        HttpResponse.json(
          { detail: "Caller lacks the 'conversations:update' permission." },
          { status: 403 },
        ),
      ),
    )
    const user = userEvent.setup()
    renderDialog({ env: 'prod' })
    await editValueAndSave(user, 'staging')

    expect(await screen.findByText(/don't have permission to do that/i)).toBeInTheDocument()
    expect(screen.queryByText(/Caller lacks/)).toBeNull()
  })

  it('reports a validation failure once — inline, with no toast beside it', async () => {
    // The app's own client, not a bare one: the suppression lives in its MutationCache, so a bare
    // QueryClient would pass this test with the opt-out deleted.
    server.use(
      http.patch(PATCH_URL, () =>
        HttpResponse.json(
          { detail: 'Tag key(s) not allowed for this evaluation: legacy.' },
          { status: 400 },
        ),
      ),
    )
    const user = userEvent.setup()
    render(dialog({ legacy: 'x' }), {
      wrapper: ({ children }: { children: ReactNode }) => (
        <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
      ),
    })
    await editValueAndSave(user, 'y')

    expect(await screen.findByText(/not allowed for this evaluation/i)).toBeInTheDocument()
    expect(toast.error).not.toHaveBeenCalled()
    queryClient.clear()
  })
})
