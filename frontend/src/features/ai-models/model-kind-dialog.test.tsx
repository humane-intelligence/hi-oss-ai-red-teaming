import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ModelKindDialog } from './model-kind-dialog'

function renderDialog(onChoose = vi.fn()) {
  render(<ModelKindDialog open onOpenChange={() => {}} onChoose={onChoose} />)
  return onChoose
}

describe('ModelKindDialog', () => {
  it('offers both kinds with the providers each one covers', () => {
    renderDialog()

    const managed = screen.getByRole('button', { name: /Provider API/ })
    const custom = screen.getByRole('button', { name: /Your own endpoint/ })
    // The provider names are the practical disambiguator — "is Azure in here?".
    expect(managed).toHaveTextContent('AWS Bedrock')
    expect(custom).toHaveTextContent('Azure')
    expect(custom).toHaveTextContent('OpenRouter')
    expect(managed).not.toHaveTextContent('Generic')
  })

  it('names each card by its title alone, not by the whole card', () => {
    renderDialog()

    // The description and provider list are the card's description, not its name — an
    // exact-name query would fail if they leaked back into the name computation.
    expect(screen.getByRole('button', { name: 'Provider API' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Your own endpoint' })).toBeInTheDocument()
  })

  it('reports the chosen kind', async () => {
    const onChoose = renderDialog()
    const user = userEvent.setup()

    await user.click(screen.getByRole('button', { name: /Your own endpoint/ }))

    expect(onChoose).toHaveBeenCalledWith('custom')
  })
})
