import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent, { type UserEvent } from '@testing-library/user-event'
import { TagKeyField } from './tag-key-field'

const trigger = () => screen.getByRole('combobox', { name: 'key' })

// The list is a portalled popover, so it has to be opened before its items exist.
const options = async (user: UserEvent) => {
  await user.click(trigger())
  const items = screen.getAllByRole('option').map((o) => o.textContent)
  await user.keyboard('{Escape}')
  return items
}

describe('TagKeyField', () => {
  it('is a free-text input while the evaluation leaves tags free-form', async () => {
    const onChange = vi.fn()
    const user = userEvent.setup()
    render(
      <TagKeyField
        value=""
        onChange={onChange}
        allowedKeys={null}
        takenKeys={[]}
        ariaLabel="key"
        keysStatus="success"
      />,
    )

    await user.type(screen.getByRole('textbox', { name: 'key' }), 'x')

    expect(screen.queryByRole('combobox')).toBeNull()
    expect(onChange).toHaveBeenCalledWith('x')
  })

  it('offers only the allowed keys once the evaluation restricts tags', async () => {
    const user = userEvent.setup()
    render(
      <TagKeyField
        value=""
        onChange={vi.fn()}
        allowedKeys={['env', 'owner']}
        takenKeys={[]}
        ariaLabel="key"
        keysStatus="success"
      />,
    )

    expect(screen.queryByRole('textbox')).toBeNull()
    expect(trigger()).toHaveTextContent('— select key —')
    expect(await options(user)).toEqual(['— select key —', 'env', 'owner'])
  })

  // `main`'s native `<option value="">` was selectable; the Radix placeholder is not, so without a
  // sentinel item a picked key could only be undone by deleting the whole row.
  it('shows the no-key row as muted, with no dead placeholder left behind', async () => {
    const user = userEvent.setup()
    render(
      <TagKeyField
        value=""
        onChange={vi.fn()}
        allowedKeys={['env']}
        takenKeys={[]}
        ariaLabel="key"
        keysStatus="success"
      />,
    )

    // With the sentinel the value is never '', so Radix never reaches a placeholder: the row
    // itself has to carry the muted colour.
    await user.click(trigger())
    const none = await screen.findByRole('option', { name: '— select key —' })
    expect(none).toHaveClass('text-muted-foreground')
  })

  it('clears a picked key back to empty', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(
      <TagKeyField
        value="env"
        onChange={onChange}
        allowedKeys={['env', 'owner']}
        takenKeys={[]}
        ariaLabel="key"
        keysStatus="success"
      />,
    )

    expect(trigger()).toHaveTextContent('env')

    await user.click(trigger())
    await user.click(await screen.findByRole('option', { name: '— select key —' }))

    // Mapped back at the boundary: the caller never sees the sentinel.
    expect(onChange).toHaveBeenCalledWith('')
  })

  it('drops keys a sibling row already uses, so the saved map cannot lose a tag', async () => {
    const user = userEvent.setup()
    // The map is keyed: two rows on 'env' would collapse to one, silently discarding the other.
    render(
      <TagKeyField
        value=""
        onChange={vi.fn()}
        allowedKeys={['env', 'owner']}
        takenKeys={['env']}
        ariaLabel="key"
        keysStatus="success"
      />,
    )

    expect(await options(user)).toEqual(['— select key —', 'owner'])
  })

  it('keeps a no-longer-allowed key selectable and labelled', async () => {
    const user = userEvent.setup()
    // Restriction turned on (or the key was removed) after the tag was authored. Hiding it would
    // drop it from the next full-map write; the backend rejects that write until the user removes it.
    render(
      <TagKeyField
        value="legacy"
        onChange={vi.fn()}
        allowedKeys={['env']}
        takenKeys={[]}
        ariaLabel="key"
        keysStatus="success"
      />,
    )

    expect(trigger()).toHaveTextContent('legacy (no longer allowed)')
    expect(await options(user)).toEqual(['— select key —', 'legacy (no longer allowed)', 'env'])
  })

  it('offers a key a sibling row holds without calling it "no longer allowed"', async () => {
    const user = userEvent.setup()
    // `options` also subtracts `takenKeys`, so deriving the label from it labels an *allowed* key as
    // withdrawn. The callers can't produce two rows on one key today, but the prop can, and the label
    // is a claim about the evaluation's schema — not about what another row is using.
    render(
      <TagKeyField
        value="env"
        onChange={vi.fn()}
        allowedKeys={['env']}
        takenKeys={['env']}
        ariaLabel="key"
        keysStatus="success"
      />,
    )

    expect(trigger()).toHaveTextContent('env')
    expect(await options(user)).toEqual(['— select key —', 'env'])
  })

  it.each(['pending', 'error'] as const)(
    'keeps the row own key selectable, unlabelled, while the list is %s',
    async (keysStatus) => {
      const user = userEvent.setup()
      // An unsettled query leaves `allowedKeys` as `[]`, so no option carries the row's key and a
      // controlled select falls back to its placeholder: the stored tag renders as having no key at
      // all, and the value beside it belongs to a tag the operator can no longer name. Unlabelled
      // because only a settled list can say a key was withdrawn.
      render(
        <TagKeyField
          value="legacy"
          onChange={vi.fn()}
          allowedKeys={[]}
          takenKeys={[]}
          ariaLabel="key"
          keysStatus={keysStatus}
        />,
      )

      expect(trigger()).toHaveTextContent('legacy')
      expect(await options(user)).toEqual(['— select key —', 'legacy'])
    },
  )
})
