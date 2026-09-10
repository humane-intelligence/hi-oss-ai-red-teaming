import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Dropdown } from './dropdown'

function Fixture() {
  return (
    <div>
      <Dropdown label="Menu" ariaLabel="Open menu">
        {(close) => (
          <button type="button" onClick={close}>
            Pick me
          </button>
        )}
      </Dropdown>
      <button type="button">Outside</button>
    </div>
  )
}

describe('Dropdown', () => {
  it('is closed initially and opens on trigger click', async () => {
    const user = userEvent.setup()
    render(<Fixture />)
    const trigger = screen.getByRole('button', { name: 'Open menu' })
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('Pick me')).toBeNull()

    await user.click(trigger)
    expect(trigger).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('Pick me')).toBeInTheDocument()
  })

  it('closes on Escape', async () => {
    const user = userEvent.setup()
    render(<Fixture />)
    await user.click(screen.getByRole('button', { name: 'Open menu' }))
    await user.keyboard('{Escape}')
    expect(screen.queryByText('Pick me')).toBeNull()
  })

  it('closes on outside click', async () => {
    const user = userEvent.setup()
    render(<Fixture />)
    await user.click(screen.getByRole('button', { name: 'Open menu' }))
    await user.click(screen.getByRole('button', { name: 'Outside' }))
    expect(screen.queryByText('Pick me')).toBeNull()
  })

  it('returns focus to the trigger when closed via Escape', async () => {
    const user = userEvent.setup()
    render(<Fixture />)
    const trigger = screen.getByRole('button', { name: 'Open menu' })
    await user.click(trigger)
    await user.keyboard('{Escape}')
    expect(trigger).toHaveFocus()
  })

  it('does not steal focus back to the trigger on outside click', async () => {
    const user = userEvent.setup()
    render(<Fixture />)
    await user.click(screen.getByRole('button', { name: 'Open menu' }))
    const outside = screen.getByRole('button', { name: 'Outside' })
    await user.click(outside)
    expect(outside).toHaveFocus()
  })

  it('closes when focus tabs out of the panel', async () => {
    const user = userEvent.setup()
    render(<Fixture />)
    await user.click(screen.getByRole('button', { name: 'Open menu' }))
    await user.tab() // trigger → panel button
    expect(screen.getByText('Pick me')).toBeInTheDocument()
    await user.tab() // panel button → sibling outside the panel
    expect(screen.queryByText('Pick me')).toBeNull()
  })

  it('stays open when focus drops to <body> (null relatedTarget)', async () => {
    // Safari (and Firefox for checkboxes) emit focusout with relatedTarget=null
    // on a label click that doesn't focus the control; closing there would kill
    // the toggle before it applies. Keyboard tab-out keeps a real relatedTarget.
    const user = userEvent.setup()
    render(<Fixture />)
    const trigger = screen.getByRole('button', { name: 'Open menu' })
    await user.click(trigger)
    fireEvent.focusOut(trigger) // no relatedTarget → null, focus dropped to <body>
    expect(screen.getByText('Pick me')).toBeInTheDocument()
  })

  it('marks the panel click-focusable', async () => {
    // Pressing a non-focusable row (a <label>) focuses the nearest click-focusable ancestor. This
    // attribute makes that the panel rather than something outside — a caller nesting the dropdown
    // in a tab panel would otherwise land focus there and get closed mid-press, killing the click.
    // jsdom runs no click-focus walk, so only the attribute is pinned here; the end-to-end path is
    // browser-verified.
    const user = userEvent.setup()
    render(<Fixture />)
    await user.click(screen.getByRole('button', { name: 'Open menu' }))

    expect(screen.getByRole('button', { name: 'Pick me' }).parentElement).toHaveAttribute(
      'tabindex',
      '-1',
    )
  })

  it('passes close() to the panel so actions can dismiss it', async () => {
    const user = userEvent.setup()
    render(<Fixture />)
    await user.click(screen.getByRole('button', { name: 'Open menu' }))
    await user.click(screen.getByText('Pick me'))
    expect(screen.queryByText('Pick me')).toBeNull()
  })

  it('calls onOpenChange on open and close, not on mount', async () => {
    const user = userEvent.setup()
    const onOpenChange = vi.fn()
    render(
      <Dropdown label="Menu" ariaLabel="Open menu" onOpenChange={onOpenChange}>
        {() => <span>Content</span>}
      </Dropdown>,
    )
    expect(onOpenChange).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: 'Open menu' }))
    expect(onOpenChange).toHaveBeenLastCalledWith(true)

    await user.keyboard('{Escape}')
    expect(onOpenChange).toHaveBeenLastCalledWith(false)
  })

  it('does not open when disabled', async () => {
    const user = userEvent.setup()
    render(
      <Dropdown label="Menu" ariaLabel="Open menu" disabled>
        {() => <span>Content</span>}
      </Dropdown>,
    )
    await user.click(screen.getByRole('button', { name: 'Open menu' }))
    expect(screen.queryByText('Content')).toBeNull()
  })
})

describe('Dropdown — viewport clamp', () => {
  // jsdom lays nothing out, so the geometry is injected: the panel reports a fixed rect and the
  // window a fixed width. That is enough to exercise the arithmetic and the re-measure wiring —
  // it says nothing about real layout, which is a browser concern.
  function stubGeometry(rect: { left: number; right: number }, innerWidth = 375) {
    const original = Element.prototype.getBoundingClientRect
    Element.prototype.getBoundingClientRect = function () {
      return this.getAttribute('role') === 'button' || this.tagName === 'BUTTON'
        ? ({ left: 0, right: 40, top: 0, bottom: 20, width: 40, height: 20 } as DOMRect)
        : ({ ...rect, top: 0, bottom: 100, width: rect.right - rect.left, height: 100 } as DOMRect)
    }
    const originalWidth = window.innerWidth
    Object.defineProperty(window, 'innerWidth', { value: innerWidth, configurable: true })
    return () => {
      Element.prototype.getBoundingClientRect = original
      Object.defineProperty(window, 'innerWidth', { value: originalWidth, configurable: true })
    }
  }

  let restore = () => {}
  afterEach(() => {
    restore()
    restore = () => {}
  })

  it('shifts a panel that overhangs the right edge back inside', async () => {
    restore = stubGeometry({ left: 200, right: 420 }) // 420 > 375 - 8
    const user = userEvent.setup()
    render(<Fixture />)

    await user.click(screen.getByRole('button', { name: 'Open menu' }))

    // right 420 vs the 367 limit → -53
    expect(screen.getByRole('button', { name: 'Pick me' }).parentElement).toHaveStyle(
      'transform: translateX(-53px)',
    )
  })

  it('shifts a panel that overhangs the left edge back inside', async () => {
    restore = stubGeometry({ left: -20, right: 200 })
    const user = userEvent.setup()
    render(<Fixture />)

    await user.click(screen.getByRole('button', { name: 'Open menu' }))

    expect(screen.getByRole('button', { name: 'Pick me' }).parentElement).toHaveStyle(
      'transform: translateX(28px)',
    )
  })

  it('leaves a panel that already fits untouched', async () => {
    restore = stubGeometry({ left: 40, right: 300 })
    const user = userEvent.setup()
    render(<Fixture />)

    await user.click(screen.getByRole('button', { name: 'Open menu' }))

    expect(screen.getByRole('button', { name: 'Pick me' }).parentElement?.style.transform).toBe('')
  })

  it('re-measures when the panel itself changes size after opening', async () => {
    // The case a measure-once clamp misses: content arrives (or changes) while the panel is open.
    const observed: Element[] = []
    let trigger: (() => void) | undefined
    const original = globalThis.ResizeObserver
    globalThis.ResizeObserver = class {
      constructor(cb: ResizeObserverCallback) {
        trigger = () => cb([], this as unknown as ResizeObserver)
      }
      observe(el: Element) {
        observed.push(el)
      }
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver

    let restoreGeometry = stubGeometry({ left: 40, right: 300 }) // fits on open
    restore = () => {
      restoreGeometry()
      globalThis.ResizeObserver = original
    }
    const user = userEvent.setup()
    render(<Fixture />)
    await user.click(screen.getByRole('button', { name: 'Open menu' }))
    const panel = screen.getByRole('button', { name: 'Pick me' }).parentElement
    expect(panel?.style.transform).toBe('')
    expect(observed).toContain(panel)

    // The panel grows past the right edge; only the observer can notice.
    restoreGeometry()
    restoreGeometry = stubGeometry({ left: 200, right: 420 })
    trigger?.()

    expect(panel).toHaveStyle('transform: translateX(-53px)')
  })
})
