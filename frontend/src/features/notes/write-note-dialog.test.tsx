import { StrictMode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { toast } from 'sonner'
import { server } from '@/test/msw/server'
import { WriteNoteDialog } from './write-note-dialog'

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

const URL = 'http://localhost/api/v1/notes'
const CONV_ID = 'conv-0001-0000-0000-000000000000'
const MSG_ID = 'msg-0001-0000-0000-000000000000'

function renderDialog(strict = false) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const ui = (
    <QueryClientProvider client={qc}>
      <WriteNoteDialog
        conversationId={CONV_ID}
        messageIds={[MSG_ID]}
        open
        onOpenChange={() => {}}
      />
    </QueryClientProvider>
  )
  return render(strict ? <StrictMode>{ui}</StrictMode> : ui)
}

async function typeAndSave(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText('Note'), 'a note')
  await user.click(screen.getByRole('button', { name: /save note/i }))
}

describe('WriteNoteDialog — a write in flight', () => {
  it('disables Cancel while saving and reports the failure inline', async () => {
    // Cancel closing mid-write would unmount the only surface that reports the failure: the
    // mutation opts out of the global toast.
    let release: () => void = () => {}
    server.use(
      http.post(URL, async () => {
        await new Promise<void>((resolve) => {
          release = resolve
        })
        return HttpResponse.json({ title: 'Server error', status: 500 }, { status: 500 })
      }),
    )
    // StrictMode on purpose: mount → cleanup → mount is what makes the ref's re-arm load-bearing.
    // Without it the flag is stuck `false` after the second mount and this failure would toast
    // instead of landing inline, so rendering plainly here would not discriminate the fix.
    const user = userEvent.setup()
    renderDialog(true)

    await typeAndSave(user)
    await waitFor(() => expect(screen.getByRole('button', { name: /saving/i })).toBeDisabled())
    expect(screen.getByRole('button', { name: /^cancel$/i })).toBeDisabled()

    release()
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(toast.error).not.toHaveBeenCalled()
  })

  it('falls back to a toast when the dialog is gone before the failure lands', async () => {
    // StrictMode on purpose: a "mounted" flag cleared only by an unmount cleanup is already
    // false on the first commit, which is what the ref's re-arming guards against.
    let release: () => void = () => {}
    server.use(
      http.post(URL, async () => {
        await new Promise<void>((resolve) => {
          release = resolve
        })
        return HttpResponse.json({ title: 'Server error', status: 500 }, { status: 500 })
      }),
    )
    const user = userEvent.setup()
    const { unmount } = renderDialog(true)

    await typeAndSave(user)
    await waitFor(() => expect(screen.getByRole('button', { name: /saving/i })).toBeDisabled())

    unmount()
    release()

    await waitFor(() => expect(toast.error).toHaveBeenCalled())
  })

  it('reports a 422 on a field it does not render instead of swallowing it', async () => {
    // `text` is the only field with a renderer. A 422 naming `message_ids` (the selection cap)
    // must reach the root alert — mapping it onto an invisible field makes Save a silent no-op.
    server.use(
      http.post(URL, () =>
        HttpResponse.json(
          {
            title: 'Validation error',
            status: 422,
            errors: [
              {
                loc: ['body', 'message_ids'],
                msg: 'List should have at most 500 items',
                type: 'too_long',
              },
            ],
          },
          { status: 422, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const user = userEvent.setup()
    renderDialog()

    await typeAndSave(user)

    const alert = await screen.findByRole('alert')
    expect(alert).toBeInTheDocument()
    // The note is still there to retry with — nothing was silently dropped.
    expect(screen.getByLabelText('Note')).toHaveValue('a note')
  })
})
