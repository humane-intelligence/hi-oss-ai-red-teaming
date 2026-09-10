import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Pencil, Trash2 } from 'lucide-react'
import { PageActions } from './page-actions'

const actions = (onSelect = vi.fn()) => [
  { key: 'edit', label: 'Edit', icon: Pencil, onSelect },
  { key: 'delete', label: 'Delete', icon: Trash2, onSelect, destructive: true },
]

describe('PageActions', () => {
  it('renders the primary cluster verbatim', () => {
    render(<PageActions primary={<button type="button">Publish</button>} secondary={actions()} />)

    expect(screen.getByRole('button', { name: 'Publish' })).toBeInTheDocument()
  })

  it('drops secondary actions whose `when` is false', () => {
    render(<PageActions secondary={[{ ...actions()[0]!, when: false }, actions()[1]!]} />)

    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Delete' })).toBeInTheDocument()
  })

  it('renders no menu trigger when no secondary action applies', () => {
    render(
      <PageActions
        primary={<button type="button">Publish</button>}
        secondary={[{ ...actions()[0]!, when: false }]}
        menuLabel="More actions"
      />,
    )

    expect(screen.queryByRole('button', { name: 'More actions' })).toBeNull()
  })

  // The inline branch used to destructure without `destructive`, so the same action read neutral
  // inline and red in the menu.
  it('marks a destructive action on both the inline button and the menu item', async () => {
    const user = userEvent.setup()
    render(<PageActions secondary={actions()} menuLabel="More actions" />)

    expect(screen.getByRole('button', { name: 'Delete' })).toHaveClass('text-destructive')
    expect(screen.getByRole('button', { name: 'Edit' })).not.toHaveClass('text-destructive')

    await user.click(screen.getByRole('button', { name: 'More actions' }))

    expect(await screen.findByRole('menuitem', { name: 'Delete' })).toHaveAttribute(
      'data-variant',
      'destructive',
    )
    expect(screen.getByRole('menuitem', { name: 'Edit' })).toHaveAttribute(
      'data-variant',
      'default',
    )
  })

  it('fires the same handler from the inline button and the menu item', async () => {
    const user = userEvent.setup()
    const onSelect = vi.fn()
    render(<PageActions secondary={[actions(onSelect)[0]!]} menuLabel="More actions" />)

    await user.click(screen.getByRole('button', { name: 'Edit' }))
    expect(onSelect).toHaveBeenCalledOnce()

    await user.click(screen.getByRole('button', { name: 'More actions' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Edit' }))
    expect(onSelect).toHaveBeenCalledTimes(2)
  })

  it('keeps a `title` on the inline button only, where a pointer can reach it', () => {
    render(<PageActions secondary={[{ ...actions()[0]!, title: 'Why this is disabled' }]} />)

    expect(screen.getByRole('button', { name: 'Edit' })).toHaveAttribute(
      'title',
      'Why this is disabled',
    )
  })
})
