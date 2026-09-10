import { describe, expect, it, vi } from 'vitest'
import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { chooseOption } from '@/test/select'
import { renderWithProviders } from '@/test/utils'
import { MAX_TAGS } from '@/lib/api/limits'
import { ChatComposer } from './chat-composer'

describe('ChatComposer — per-message tags', () => {
  it('sends tags entered in the Tags editor', async () => {
    const send = vi.fn().mockResolvedValue(true)
    const user = userEvent.setup()
    renderWithProviders(
      <ChatComposer
        send={send}
        pending={false}
        acceptsImages={false}
        tagsEnabled
        allowedTagKeys={null}
        allowedKeysStatus="success"
        placeholder="msg"
      />,
    )

    await user.click(screen.getByRole('button', { name: /^tags/i }))
    await user.type(screen.getByLabelText('Message tag 1 key'), 'turn')
    await user.type(screen.getByLabelText('Message tag 1 value'), '1')
    await user.type(screen.getByRole('textbox', { name: 'Message' }), 'hi')
    await user.click(screen.getByRole('button', { name: /^send$/i }))

    expect(send).toHaveBeenCalledWith('hi', [], { turn: '1' })
  })

  it('reports the disclosure state of the Tags panel', async () => {
    const user = userEvent.setup()
    renderWithProviders(
      <ChatComposer
        send={vi.fn()}
        pending={false}
        acceptsImages={false}
        tagsEnabled
        allowedTagKeys={null}
        allowedKeysStatus="success"
        placeholder="msg"
      />,
    )

    const toggle = screen.getByRole('button', { name: /^tags/i })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')

    await user.click(toggle)

    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    // Without the pairing a screen-reader user can hear the state but not reach what it controls.
    const panelId = toggle.getAttribute('aria-controls')
    expect(panelId).toBeTruthy()
    expect(document.getElementById(panelId as string)).toContainElement(
      screen.getByLabelText('Message tag 1 key'),
    )
  })

  it('locks the tag rows while a turn is in flight', async () => {
    // A tag edited mid-stream can't reach the in-flight turn, so offering the edit misreports it —
    // the attachment strip is disabled for the same reason.
    const user = userEvent.setup()
    renderWithProviders(
      <ChatComposer
        send={vi.fn()}
        pending
        acceptsImages={false}
        tagsEnabled
        allowedTagKeys={null}
        allowedKeysStatus="success"
        placeholder="msg"
      />,
    )
    await user.click(screen.getByRole('button', { name: /^tags/i }))

    expect(screen.getByLabelText('Message tag 1 key')).toBeDisabled()
    expect(screen.getByLabelText('Message tag 1 value')).toBeDisabled()
    expect(screen.getByRole('button', { name: /^add tag$/i })).toBeDisabled()
    expect(screen.getByRole('button', { name: /remove message tag 1/i })).toBeDisabled()
  })

  it('blocks the send and names the row when a value has no key', async () => {
    const send = vi.fn().mockResolvedValue(true)
    const user = userEvent.setup()
    renderWithProviders(
      <ChatComposer
        send={send}
        pending={false}
        acceptsImages={false}
        tagsEnabled
        allowedTagKeys={null}
        allowedKeysStatus="success"
        placeholder="msg"
      />,
    )

    await user.type(screen.getByRole('textbox', { name: 'Message' }), 'hi')
    await user.click(screen.getByRole('button', { name: /^tags/i }))
    await user.type(screen.getByLabelText('Message tag 1 value'), 'never respond in English')
    await user.click(screen.getByRole('button', { name: /^send$/i }))

    expect(await screen.findByText('Row 1: key is required')).toBeInTheDocument()
    expect(send).not.toHaveBeenCalled()
    // The message is persisted immutably, so losing this silently under a success signal is the bug.
    expect(screen.getByLabelText('Message tag 1 value')).toHaveValue('never respond in English')
  })

  it('blocks the send and names the colliding key when two rows share one', async () => {
    const send = vi.fn().mockResolvedValue(true)
    const user = userEvent.setup()
    renderWithProviders(
      <ChatComposer
        send={send}
        pending={false}
        acceptsImages={false}
        tagsEnabled
        allowedTagKeys={null}
        allowedKeysStatus="success"
        placeholder="msg"
      />,
    )

    await user.type(screen.getByRole('textbox', { name: 'Message' }), 'hi')
    await user.click(screen.getByRole('button', { name: /^tags/i }))
    await user.type(screen.getByLabelText('Message tag 1 key'), 'env')
    await user.type(screen.getByLabelText('Message tag 1 value'), 'prod')
    await user.click(screen.getByRole('button', { name: /^add tag$/i }))
    await user.type(screen.getByLabelText('Message tag 2 key'), 'env')
    await user.type(screen.getByLabelText('Message tag 2 value'), 'staging')
    await user.click(screen.getByRole('button', { name: /^send$/i }))

    expect(await screen.findByText('Duplicate key "env"')).toBeInTheDocument()
    expect(send).not.toHaveBeenCalled()
  })

  it('drops blank-key rows and clears the editor after send', async () => {
    const send = vi.fn().mockResolvedValue(true)
    const user = userEvent.setup()
    renderWithProviders(
      <ChatComposer
        send={send}
        pending={false}
        acceptsImages={false}
        tagsEnabled
        allowedTagKeys={null}
        allowedKeysStatus="success"
        placeholder="msg"
      />,
    )

    await user.click(screen.getByRole('button', { name: /^tags/i }))
    await user.type(screen.getByLabelText('Message tag 1 key'), 'env')
    await user.type(screen.getByLabelText('Message tag 1 value'), 'prod')
    await user.click(screen.getByRole('button', { name: /^add tag$/i })) // second, left-blank row
    await user.type(screen.getByRole('textbox', { name: 'Message' }), 'hi')
    await user.click(screen.getByRole('button', { name: /^send$/i }))

    expect(send).toHaveBeenCalledWith('hi', [], { env: 'prod' }) // blank row dropped
    // editor collapses + resets after a successful send
    expect(screen.queryByLabelText('Message tag 1 key')).toBeNull()
  })

  it('renders no tag editor when the evaluation has tagging disabled', async () => {
    const send = vi.fn().mockResolvedValue(true)
    const user = userEvent.setup()
    renderWithProviders(
      <ChatComposer
        send={send}
        pending={false}
        acceptsImages={false}
        tagsEnabled={false}
        allowedTagKeys={null}
        allowedKeysStatus="success"
        placeholder="msg"
      />,
    )

    expect(screen.queryByRole('button', { name: /^tags/i })).toBeNull()
    await user.type(screen.getByRole('textbox', { name: 'Message' }), 'hi')
    await user.click(screen.getByRole('button', { name: /^send$/i }))

    expect(send).toHaveBeenCalledWith('hi', [], {})
  })

  it('picks keys from a select and stops offering "Add tag" once they run out', async () => {
    const send = vi.fn().mockResolvedValue(true)
    const user = userEvent.setup()
    renderWithProviders(
      <ChatComposer
        send={send}
        pending={false}
        acceptsImages={false}
        tagsEnabled
        allowedTagKeys={['env']}
        allowedKeysStatus="success"
        placeholder="msg"
      />,
    )

    await user.click(screen.getByRole('button', { name: /^tags/i }))
    // Restricted → no free-text key field; the one allowed key is offered.
    expect(screen.queryByRole('textbox', { name: 'Message tag 1 key' })).toBeNull()
    await chooseOption(user, 'Message tag 1 key', /^env$/)
    await user.type(screen.getByLabelText('Message tag 1 value'), 'prod')
    // 'env' is now taken, so there is no second key to add — refused with the reason on the page, and
    // still reachable by keyboard (see the refusal contract shared with the Edit-tags dialog).
    const addTag = screen.getByRole('button', { name: /^add tag$/i })
    expect(addTag).toHaveAttribute('aria-disabled', 'true')
    expect(
      document.getElementById(addTag.getAttribute('aria-describedby') as string),
    ).toHaveTextContent(/already used/i)
    await user.type(screen.getByRole('textbox', { name: 'Message' }), 'hi')
    await user.click(screen.getByRole('button', { name: /^send$/i }))

    expect(send).toHaveBeenCalledWith('hi', [], { env: 'prod' })
  })

  it('sends an empty map when no tags are entered', async () => {
    const send = vi.fn().mockResolvedValue(true)
    const user = userEvent.setup()
    renderWithProviders(
      <ChatComposer
        send={send}
        pending={false}
        acceptsImages={false}
        tagsEnabled
        allowedTagKeys={null}
        allowedKeysStatus="success"
        placeholder="msg"
      />,
    )

    await user.type(screen.getByRole('textbox', { name: 'Message' }), 'hi')
    await user.click(screen.getByRole('button', { name: /^send$/i }))

    expect(send).toHaveBeenCalledWith('hi', [], {})
  })
})

describe('ChatComposer — refusing another row', () => {
  it.each(['pending', 'error'] as const)(
    'refuses a new row while the list is %s, and points at the line that says why',
    async (status) => {
      // `[].every()` is vacuously true, so an unsettled list read as "every allowed key is already
      // used" — a false reason. A new row is still refused (its picker would be empty), but the button
      // points at the loading/error line instead of inventing one, and stays reachable by keyboard.
      const user = userEvent.setup()
      renderWithProviders(
        <ChatComposer
          send={vi.fn()}
          pending={false}
          acceptsImages={false}
          tagsEnabled
          allowedTagKeys={[]}
          allowedKeysStatus={status}
          placeholder="msg"
        />,
      )
      await user.click(screen.getByRole('button', { name: /^tags/i }))

      const addTag = screen.getByRole('button', { name: /^add tag$/i })
      expect(addTag).toHaveAttribute('aria-disabled', 'true')
      expect(screen.queryByText(/already used/i)).toBeNull()
      const reasonId = addTag.getAttribute('aria-describedby')
      expect(document.getElementById(reasonId as string)).toHaveTextContent(/see the note above/i)
    },
  )

  it('names the empty seeded row as the reason for refusing another', async () => {
    // Opening the panel seeds one blank row, so the very first refusal an operator can meet is this
    // one — and it has to say so rather than leaving a dead control (the dialog's contract).
    const user = userEvent.setup()
    renderWithProviders(
      <ChatComposer
        send={vi.fn()}
        pending={false}
        acceptsImages={false}
        tagsEnabled
        allowedTagKeys={null}
        allowedKeysStatus="success"
        placeholder="msg"
      />,
    )
    await user.click(screen.getByRole('button', { name: /^tags/i }))

    const addTag = screen.getByRole('button', { name: /^add tag$/i })
    expect(addTag).toHaveAttribute('aria-disabled', 'true')
    const reasonId = addTag.getAttribute('aria-describedby')
    expect(document.getElementById(reasonId as string)).toHaveTextContent(
      /fill the empty row first/i,
    )
  })

  it('refuses a new row at the cap, and says so', async () => {
    // The composer's own cap arm: MAX_TAGS keyed rows and no blank one left, which is the only state
    // where the cap is the true reason (a blank row would otherwise be named first).
    const user = userEvent.setup()
    renderWithProviders(
      <ChatComposer
        send={vi.fn()}
        pending={false}
        acceptsImages={false}
        tagsEnabled
        allowedTagKeys={null}
        allowedKeysStatus="success"
        placeholder="msg"
      />,
    )
    await user.click(screen.getByRole('button', { name: /^tags/i }))
    for (let i = 1; i <= MAX_TAGS; i++) {
      if (i > 1) await user.click(screen.getByRole('button', { name: /^add tag$/i }))
      await user.type(screen.getByLabelText(`Message tag ${i} key`), `k${i}`)
      await user.type(screen.getByLabelText(`Message tag ${i} value`), 'v')
    }

    const addTag = screen.getByRole('button', { name: /^add tag$/i })
    expect(addTag).toHaveAttribute('aria-disabled', 'true')
    expect(
      document.getElementById(addTag.getAttribute('aria-describedby') as string),
    ).toHaveTextContent(new RegExp(`up to ${MAX_TAGS} tags per message`, 'i'))
  })

  it('does not claim keys are exhausted when the evaluation allows none', async () => {
    // `[].every()` is vacuously true, so a settled-but-empty allow-list read as "every allowed key is
    // already used" — about an evaluation that allows none. Its own line above says what is true.
    const user = userEvent.setup()
    renderWithProviders(
      <ChatComposer
        send={vi.fn()}
        pending={false}
        acceptsImages={false}
        tagsEnabled
        allowedTagKeys={[]}
        allowedKeysStatus="success"
        placeholder="msg"
      />,
    )
    await user.click(screen.getByRole('button', { name: /^tags/i }))

    expect(screen.getByText(/restricts tags and allows no keys yet/i)).toBeInTheDocument()
    const addTag = screen.getByRole('button', { name: /^add tag$/i })
    expect(screen.queryByText(/already used/i)).toBeNull()
    expect(
      document.getElementById(addTag.getAttribute('aria-describedby') as string),
    ).toHaveTextContent(/see the note above/i)
  })

  it('clears a row-indexed message when a row is added', async () => {
    // The message names a row by its index, so adding one shifts what it points at. The dialog clears
    // on every row edit for the same reason; this is the composer's half of that rule.
    const send = vi.fn().mockResolvedValue(true)
    const user = userEvent.setup()
    renderWithProviders(
      <ChatComposer
        send={send}
        pending={false}
        acceptsImages={false}
        tagsEnabled
        allowedTagKeys={null}
        allowedKeysStatus="success"
        placeholder="msg"
      />,
    )
    await user.type(screen.getByRole('textbox', { name: 'Message' }), 'hi')
    await user.click(screen.getByRole('button', { name: /^tags/i }))
    await user.type(screen.getByLabelText('Message tag 1 value'), 'orphaned')
    await user.click(screen.getByRole('button', { name: /^send$/i }))
    expect(await screen.findByText('Row 1: key is required')).toBeInTheDocument()

    await user.type(screen.getByLabelText('Message tag 1 key'), 'env')
    await user.click(screen.getByRole('button', { name: /^add tag$/i }))

    expect(screen.queryByText('Row 1: key is required')).toBeNull()
  })

  it('blocks a send whose row carries only invisible characters', async () => {
    // The server rejects such a value (422, "carries no text"); blocking here keeps that round-trip
    // off the wire and names the offending row — `trim()` alone treats a zero-width space as filled.
    const send = vi.fn().mockResolvedValue(true)
    const user = userEvent.setup()
    renderWithProviders(
      <ChatComposer
        send={send}
        pending={false}
        acceptsImages={false}
        tagsEnabled
        allowedTagKeys={null}
        allowedKeysStatus="success"
        placeholder="msg"
      />,
    )
    await user.type(screen.getByRole('textbox', { name: 'Message' }), 'hi')
    await user.click(screen.getByRole('button', { name: /^tags/i }))
    await user.type(screen.getByLabelText('Message tag 1 key'), 'note')
    await user.type(screen.getByLabelText('Message tag 1 value'), '\u200b')
    await user.click(screen.getByRole('button', { name: /^send$/i }))

    expect(await screen.findByText('Row 1: value is required')).toBeInTheDocument()
    expect(send).not.toHaveBeenCalled()
  })

  it('counts tags, not rows, when it reports the cap', async () => {
    // The cap read `tagRows.length`, so a cleared row counted against it — the same off-by-N the
    // dialog fixed. With MAX_TAGS - 1 keyed rows plus one blank the row count is at the cap while the
    // tag count is one short, so the reason has to be the blank row, not "up to N tags".
    const user = userEvent.setup()
    renderWithProviders(
      <ChatComposer
        send={vi.fn()}
        pending={false}
        acceptsImages={false}
        tagsEnabled
        allowedTagKeys={null}
        allowedKeysStatus="success"
        placeholder="msg"
      />,
    )
    await user.click(screen.getByRole('button', { name: /^tags/i }))
    for (let i = 1; i < MAX_TAGS; i++) {
      if (i > 1) await user.click(screen.getByRole('button', { name: /^add tag$/i }))
      await user.type(screen.getByLabelText(`Message tag ${i} key`), `k${i}`)
      await user.type(screen.getByLabelText(`Message tag ${i} value`), 'v')
    }
    await user.click(screen.getByRole('button', { name: /^add tag$/i }))

    expect(screen.getByLabelText(`Message tag ${MAX_TAGS} key`)).toHaveValue('')
    const addTag = screen.getByRole('button', { name: /^add tag$/i })
    const reasonId = addTag.getAttribute('aria-describedby')
    expect(document.getElementById(reasonId as string)).toHaveTextContent(
      /fill the empty row first/i,
    )
  })
})
