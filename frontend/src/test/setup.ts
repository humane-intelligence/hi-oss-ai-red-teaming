import '@testing-library/jest-dom/vitest'
import { afterAll, afterEach } from 'vitest'
import { cleanup, configure } from '@testing-library/react'
import { server } from './msw/server'

// `findBy*` carries its own 1s budget, independent of vitest's `testTimeout`, too tight under load.
configure({ asyncUtilTimeout: 5_000 })

// Start MSW at the top level so globalThis.fetch is patched before any module
// that calls createClient() captures it. beforeAll() runs after module evaluation,
// which is too late for openapi-fetch's one-time fetch capture.
server.listen({ onUnhandledRequest: 'error' })

// jsdom implements no ResizeObserver; components that observe their own box (the dropdown's
// viewport clamp) construct one on open, so every test rendering one needs it to exist.
class ResizeObserverStub implements ResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver ??= ResizeObserverStub

// jsdom implements no `<dialog>` behaviour, and `ui/modal.tsx` plus `ui/drawer.tsx` are built on
// the native element, so every spec that renders one needs these. Defined here rather than in the
// 39 files that had their own copy. They stand in for the open/close bookkeeping only: the focus
// trap, Escape and focus return are the browser's, exercised in the browser pass and not here.
HTMLDialogElement.prototype.showModal ??= function showModal(this: HTMLDialogElement) {
  this.open = true
}
HTMLDialogElement.prototype.close ??= function close(this: HTMLDialogElement) {
  this.open = false
  this.dispatchEvent(new Event('close'))
}

// jsdom implements none of these; Radix's Select calls them when the listbox opens.
Element.prototype.scrollIntoView ??= () => {}
Element.prototype.hasPointerCapture ??= () => false
Element.prototype.setPointerCapture ??= () => {}
Element.prototype.releasePointerCapture ??= () => {}

afterEach(() => {
  cleanup()
  server.resetHandlers()
  localStorage.clear()
})
afterAll(() => server.close())
