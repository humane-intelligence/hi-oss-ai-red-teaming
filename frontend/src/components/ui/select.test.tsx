import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Modal } from './modal'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from './select'

const picker = () => (
  <Select defaultOpen value="a" onValueChange={() => {}}>
    <SelectTrigger aria-label="Fruit">
      <SelectValue placeholder="pick one" />
    </SelectTrigger>
    <SelectContent>
      <SelectItem value="a">Apple</SelectItem>
      <SelectItem value="b">Pear</SelectItem>
    </SelectContent>
  </Select>
)

describe('SelectContent portal container', () => {
  // The bug this covers: `showModal()` makes everything outside the dialog inert, so a listbox
  // portalled to `document.body` renders but cannot be clicked. Reading the element from context
  // rather than querying `dialog[open]` also removes the ordering trap, since the open effect runs
  // after the children have already mounted.
  it('portals into the enclosing dialog, not document.body', async () => {
    render(
      <Modal open onOpenChange={() => {}} title="Add member">
        {picker()}
      </Modal>,
    )

    const listbox = await screen.findByRole('listbox')
    const dialog = screen.getByRole('dialog')

    expect(dialog.contains(listbox)).toBe(true)
    expect(listbox.closest('dialog')).toBe(dialog)
  })

  it('portals to the body when there is no dialog around it', async () => {
    render(picker())

    const listbox = await screen.findByRole('listbox')

    expect(listbox.closest('dialog')).toBeNull()
    expect(document.body.contains(listbox)).toBe(true)
  })
})
