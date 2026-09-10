import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Modal } from './modal'

describe('Modal — busy', () => {
  it('holds a close request while a write is in flight, and lets it through when idle', () => {
    // Esc reaches the dialog element, so a disabled Cancel button does not stop it; the non-keyboard
    // route (`requestClose()`, a platform close signal) arrives as `cancel` instead. Both are held.
    const { rerender } = render(
      <Modal open busy onOpenChange={() => {}} title="Busy">
        body
      </Modal>,
    )
    const dialog = screen.getByRole('dialog')

    const esc = new KeyboardEvent('keydown', { key: 'Escape', cancelable: true, bubbles: true })
    dialog.dispatchEvent(esc)
    const cancel = new Event('cancel', { cancelable: true, bubbles: true })
    dialog.dispatchEvent(cancel)

    expect(esc.defaultPrevented).toBe(true)
    expect(cancel.defaultPrevented).toBe(true)

    rerender(
      <Modal open onOpenChange={() => {}} title="Busy">
        body
      </Modal>,
    )
    const escIdle = new KeyboardEvent('keydown', { key: 'Escape', cancelable: true, bubbles: true })
    screen.getByRole('dialog').dispatchEvent(escIdle)
    const cancelIdle = new Event('cancel', { cancelable: true, bubbles: true })
    screen.getByRole('dialog').dispatchEvent(cancelIdle)

    expect(escIdle.defaultPrevented).toBe(false)
    expect(cancelIdle.defaultPrevented).toBe(false)
  })

  it('ignores a backdrop click while busy, even when light-dismiss is enabled', async () => {
    // `dismissable` exists for low-stakes dialogs; a write in flight is not one of them.
    const onOpenChange = vi.fn()
    render(
      <Modal open busy dismissable onOpenChange={onOpenChange} title="Busy">
        body
      </Modal>,
    )
    const dialog = screen.getByRole('dialog')
    // The backdrop is the <dialog> itself, and the press has to start there — see `pressedBackdrop`.
    dialog.getBoundingClientRect = () =>
      ({ left: 100, right: 200, top: 100, bottom: 200 }) as DOMRect
    await userEvent.pointer([
      { target: dialog, coords: { clientX: 10, clientY: 10 } },
      { keys: '[MouseLeft]', target: dialog },
    ])

    expect(onOpenChange).not.toHaveBeenCalled()
  })
})

describe('Modal lazy-render', () => {
  it('does not render children when closed', () => {
    render(
      <Modal open={false} onOpenChange={() => {}} title="Test">
        <div>BODY</div>
      </Modal>,
    )
    expect(screen.queryByText('BODY')).toBeNull()
  })

  it('renders children when open', () => {
    render(
      <Modal open={true} onOpenChange={() => {}} title="Test">
        <div>BODY</div>
      </Modal>,
    )
    expect(screen.getByText('BODY')).toBeInTheDocument()
    expect(screen.getByText('Test')).toBeInTheDocument()
  })

  it('renders eyebrow when open', () => {
    render(
      <Modal open={true} onOpenChange={() => {}} title="Title" eyebrow="Subtitle">
        <div>content</div>
      </Modal>,
    )
    expect(screen.getByText('Subtitle')).toBeInTheDocument()
  })
})
