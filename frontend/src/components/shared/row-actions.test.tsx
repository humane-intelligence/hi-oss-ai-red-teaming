import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { RotateCw, Trash2 } from 'lucide-react'
import { RowActions } from './row-actions'

const actions = (onSelect: () => void = vi.fn()) => [
  {
    key: 'resend',
    label: 'Resend invitation',
    ariaLabel: 'Resend invitation to a@b.c',
    icon: RotateCw,
    onSelect,
  },
  { key: 'delete', label: 'Delete', icon: Trash2, onSelect, destructive: true },
]

describe('RowActions', () => {
  it('renders one inline button per action, named by ariaLabel where given', () => {
    render(<RowActions actions={actions()} />)

    expect(screen.getByRole('button', { name: 'Resend invitation to a@b.c' })).toBeInTheDocument()
    // No `ariaLabel`, so the label carries the name.
    expect(screen.getByRole('button', { name: 'Delete' })).toBeInTheDocument()
  })

  it('drops actions whose `when` is false, and renders nothing when none apply', () => {
    const { container } = render(
      <RowActions
        actions={[
          { ...actions()[0]!, when: false },
          { ...actions()[1]!, when: false },
        ]}
      />,
    )

    expect(container).toBeEmptyDOMElement()
  })

  it('offers every applicable action in the menu, marking the destructive one', async () => {
    const user = userEvent.setup()
    render(<RowActions actions={actions()} menuLabel="Actions for a@b.c" />)

    await user.click(screen.getByRole('button', { name: 'Actions for a@b.c' }))

    expect(await screen.findByRole('menuitem', { name: 'Resend invitation' })).toBeInTheDocument()
    const destructive = screen.getByRole('menuitem', { name: 'Delete' })
    expect(destructive).toHaveAttribute('data-variant', 'destructive')
  })

  // The regression this component shipped with: the row navigates, so an item that lets the click
  // through both leaves the page and unmounts the dialog the action just opened.
  it('does not let a menu item’s click reach the row', async () => {
    const user = userEvent.setup()
    const onSelect = vi.fn()
    const onRowClick = vi.fn()
    // The plain div stands in for the clickable table row.
    render(
      <div onClick={onRowClick}>
        <RowActions actions={actions(onSelect)} menuLabel="Actions for a@b.c" />
      </div>,
    )

    await user.click(screen.getByRole('button', { name: 'Actions for a@b.c' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Resend invitation' }))

    expect(onSelect).toHaveBeenCalledOnce()
    expect(onRowClick).not.toHaveBeenCalled()
  })

  it('does not restore focus to the trigger when an action moves it elsewhere', async () => {
    const user = userEvent.setup()
    render(
      <>
        <RowActions
          actions={actions(() => document.querySelector<HTMLElement>('#dialog-control')!.focus())}
          menuLabel="Actions for a@b.c"
        />
        <button id="dialog-control">Dialog control</button>
      </>,
    )

    await user.click(screen.getByRole('button', { name: 'Actions for a@b.c' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Resend invitation' }))

    expect(screen.getByRole('button', { name: 'Dialog control' })).toHaveFocus()
  })

  // The other half of the same handler: `preventDefault` stops Radix restoring focus, so an action
  // that opens nothing used to leave `<body>` focused with the trigger still on screen.
  it('leaves focus on the trigger when the action opens nothing', async () => {
    const user = userEvent.setup()
    const onSelect = vi.fn()
    render(
      <RowActions
        actions={[{ key: 'resend', label: 'Resend invitation', icon: RotateCw, onSelect }]}
        menuLabel="Actions for a@b.c"
      />,
    )

    const trigger = screen.getByRole('button', { name: 'Actions for a@b.c' })
    await user.click(trigger)
    await user.click(await screen.findByRole('menuitem', { name: 'Resend invitation' }))

    expect(onSelect).toHaveBeenCalledOnce()
    expect(trigger).toHaveFocus()
  })

  it('does not let an inline button’s click reach the row either', async () => {
    const user = userEvent.setup()
    const onSelect = vi.fn()
    const onRowClick = vi.fn()
    // The plain div stands in for the clickable table row.
    render(
      <div onClick={onRowClick}>
        <RowActions actions={actions(onSelect)} />
      </div>,
    )

    await user.click(screen.getByRole('button', { name: 'Resend invitation to a@b.c' }))

    expect(onSelect).toHaveBeenCalledOnce()
    expect(onRowClick).not.toHaveBeenCalled()
  })

  it('opening the menu is not itself a row click', async () => {
    const user = userEvent.setup()
    const onRowClick = vi.fn()
    // The plain div stands in for the clickable table row.
    render(
      <div onClick={onRowClick}>
        <RowActions actions={actions()} menuLabel="Actions for a@b.c" />
      </div>,
    )

    await user.click(screen.getByRole('button', { name: 'Actions for a@b.c' }))

    expect(await screen.findByRole('menu')).toBeInTheDocument()
    expect(onRowClick).not.toHaveBeenCalled()
  })

  it('keeps a disabled action unclickable on both paths', async () => {
    const user = userEvent.setup()
    const onSelect = vi.fn()
    render(
      <RowActions
        actions={[{ ...actions(onSelect)[0]!, disabled: true }]}
        menuLabel="Actions for a@b.c"
      />,
    )

    await user.click(screen.getByRole('button', { name: 'Resend invitation to a@b.c' }))
    expect(onSelect).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: 'Actions for a@b.c' }))
    const item = await screen.findByRole('menuitem', { name: 'Resend invitation' })
    expect(item).toHaveAttribute('data-disabled')
  })
})
