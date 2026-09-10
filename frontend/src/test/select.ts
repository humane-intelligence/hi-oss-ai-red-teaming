import { fireEvent, screen } from '@testing-library/react'
import type { UserEvent } from '@testing-library/user-event'

// Radix's Select is a button plus a portalled listbox, so `selectOptions` does not reach it and
// options exist only while the list is open. Both helpers open, pick, and let it close.
export async function chooseOption(
  user: UserEvent,
  trigger: string | RegExp,
  option: string | RegExp,
) {
  await user.click(screen.getByRole('combobox', { name: trigger }))
  await user.click(await screen.findByRole('option', { name: option }))
}

// Radix opens on `pointerdown`, not `click`, so a `fireEvent.click` on the trigger does nothing.
// Split out because a test that only reads the options never picks one.
export async function openSelect(label: string) {
  const trigger = await screen.findByLabelText(label)
  fireEvent.pointerDown(trigger, { button: 0, pointerType: 'mouse' })
  await screen.findByRole('listbox')
}

// Same, without a `userEvent` instance: tests that only ever used `fireEvent` need not set one up.
export async function setSelect(label: string, option: string | RegExp) {
  await openSelect(label)
  fireEvent.click(await screen.findByRole('option', { name: option }))
}
