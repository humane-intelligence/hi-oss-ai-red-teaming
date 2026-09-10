import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ParamField, SystemPromptField } from './param-field'

describe('ParamField', () => {
  it('keeps the inherited value visible while the field is being typed into', async () => {
    // The behaviour the placeholder could not provide: it vanished on the first keystroke,
    // which is exactly when the operator wants to compare their override against it.
    const user = userEvent.setup()
    render(
      <ParamField
        id="p-temperature"
        label="Temperature"
        step="any"
        inherited="0.7"
        filled={false}
        onClear={() => {}}
        inputProps={{}}
      />,
    )

    expect(screen.getByText('Inherited: 0.7')).toBeInTheDocument()
    await user.type(screen.getByLabelText('Temperature'), '1.2')
    expect(screen.getByText('Inherited: 0.7')).toBeInTheDocument()
  })

  it('renders no inherited line when nothing is inherited', () => {
    render(
      <ParamField
        id="p-temperature"
        label="Temperature"
        step="any"
        filled={false}
        onClear={() => {}}
        inputProps={{}}
      />,
    )

    expect(screen.queryByText(/^Inherited:/)).not.toBeInTheDocument()
  })

  it('hides the clear affordance from assistive tech until the field holds a value', () => {
    // `invisible` is visibility:hidden, so the button leaves the a11y tree and the tab order
    // rather than sitting there as a phantom control on every empty knob.
    const { rerender } = render(
      <ParamField
        id="p-temperature"
        label="Temperature"
        step="any"
        filled={false}
        onClear={() => {}}
        inputProps={{}}
      />,
    )
    expect(screen.getByRole('button', { name: 'Clear Temperature' })).toHaveClass('invisible')

    rerender(
      <ParamField
        id="p-temperature"
        label="Temperature"
        step="any"
        filled
        onClear={() => {}}
        inputProps={{}}
      />,
    )
    expect(screen.getByRole('button', { name: 'Clear Temperature' })).not.toHaveClass('invisible')
  })

  it('asks the host to blank the field', async () => {
    const onClear = vi.fn()
    const user = userEvent.setup()
    render(
      <ParamField
        id="p-temperature"
        label="Temperature"
        step="any"
        filled
        onClear={onClear}
        inputProps={{}}
      />,
    )

    await user.click(screen.getByRole('button', { name: 'Clear Temperature' }))
    expect(onClear).toHaveBeenCalledOnce()
  })
})

describe('SystemPromptField', () => {
  it('clamps a long inherited prompt but keeps the whole value reachable', () => {
    // A system prompt runs to hundreds of characters and this renders inside a modal.
    const prompt = 'You are a helpful assistant.'.repeat(20)
    render(
      <SystemPromptField
        id="p-system-prompt"
        inherited={prompt}
        filled={false}
        onClear={() => {}}
        inputProps={{}}
      />,
    )

    const line = screen.getByText(`Inherited: ${prompt}`)
    expect(line).toHaveClass('line-clamp-2')
    expect(line).toHaveAttribute('title', prompt)
  })

  it('offers its own clear affordance, like the numeric knobs', () => {
    render(<SystemPromptField id="p-system-prompt" filled onClear={() => {}} inputProps={{}} />)

    expect(screen.getByRole('button', { name: 'Clear System prompt' })).not.toHaveClass('invisible')
  })
})
