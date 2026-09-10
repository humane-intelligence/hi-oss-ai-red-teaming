import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Drawer } from './drawer'

// trap, Escape and focus return are NOT exercised here — they are covered in the browser pass.

// The lock effect captures body.style.overflow at mount, so a leftover value changes the next test.
afterEach(() => {
  document.body.style.overflow = ''
})

function renderDrawer(open: boolean, onOpenChange = vi.fn()) {
  const view = render(
    <Drawer open={open} onOpenChange={onOpenChange} label="Navigation">
      <a href="/x">Overview</a>
    </Drawer>,
  )
  return { ...view, onOpenChange }
}

describe('Drawer — body scroll lock', () => {
  it('locks the page while open and restores the previous value on close', () => {
    document.body.style.overflow = 'auto' // a pre-existing value the drawer must not clobber
    const { rerender } = renderDrawer(true)
    expect(document.body.style.overflow).toBe('hidden')

    rerender(
      <Drawer open={false} onOpenChange={vi.fn()} label="Navigation">
        <a href="/x">Overview</a>
      </Drawer>,
    )
    expect(document.body.style.overflow).toBe('auto')
  })

  it('restores the page when unmounted while still open', () => {
    const { unmount } = renderDrawer(true)
    expect(document.body.style.overflow).toBe('hidden')

    unmount()
    expect(document.body.style.overflow).toBe('')
  })

  it('leaves the page alone while closed', () => {
    renderDrawer(false)
    expect(document.body.style.overflow).toBe('')
  })
})

describe('Drawer — dismissal', () => {
  it('closes when both the press and the release land on the backdrop', () => {
    const { onOpenChange } = renderDrawer(true)
    const dialog = screen.getByRole('dialog')

    fireEvent.pointerDown(dialog)
    fireEvent.click(dialog)

    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('stays open when the press started inside the panel and released on the backdrop', () => {
    const { onOpenChange } = renderDrawer(true)
    const dialog = screen.getByRole('dialog')

    fireEvent.pointerDown(screen.getByRole('link', { name: 'Overview' }))
    fireEvent.click(dialog)

    expect(onOpenChange).not.toHaveBeenCalled()
  })

  it('reports a close driven by the dialog itself (what Escape triggers)', () => {
    const { onOpenChange } = renderDrawer(true)

    // The browser closes the dialog on Escape and fires `close`; this asserts our handler relays it.
    fireEvent(screen.getByRole('dialog'), new Event('close'))

    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('closes from its own close button', async () => {
    const user = userEvent.setup()
    const { onOpenChange } = renderDrawer(true)

    await user.click(screen.getByRole('button', { name: /close/i }))

    expect(onOpenChange).toHaveBeenCalledWith(false)
  })
})
