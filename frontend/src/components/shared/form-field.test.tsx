import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { FormField } from './form-field'
import { Input } from '@/components/ui/input'

describe('FormField', () => {
  it('labels the control through htmlFor', () => {
    render(
      <FormField label="Alias" htmlFor="alias">
        <Input id="alias" />
      </FormField>,
    )

    expect(screen.getByLabelText('Alias')).toBeInTheDocument()
  })

  // Nesting the hint inside the label changes its accessible text and breaks `getByLabelText`.
  it('keeps the label matchable when a hint sits beside it', () => {
    render(
      <FormField label="Alias" htmlFor="alias" hint="Some longer explanation">
        <Input id="alias" />
      </FormField>,
    )

    expect(screen.getByLabelText('Alias')).toBeInTheDocument()
  })

  it('points the control at its description without the caller wiring it', () => {
    render(
      <FormField label="Alias" htmlFor="alias" description="Shown in the picker.">
        <Input id="alias" />
      </FormField>,
    )

    const control = screen.getByLabelText('Alias')
    const id = control.getAttribute('aria-describedby')
    expect(id).toBeTruthy()
    expect(document.getElementById(id!)).toHaveTextContent('Shown in the picker.')
  })

  it('marks the control invalid and points it at the error', () => {
    render(
      <FormField label="Alias" htmlFor="alias" error="Required">
        <Input id="alias" />
      </FormField>,
    )

    const control = screen.getByLabelText('Alias')
    expect(control).toHaveAttribute('aria-invalid', 'true')
    const id = control.getAttribute('aria-describedby')
    expect(document.getElementById(id!)).toHaveTextContent('Required')
    expect(screen.getByRole('alert')).toHaveTextContent('Required')
  })

  it('references the description and the error together', () => {
    render(
      <FormField label="Alias" htmlFor="alias" description="Shown in the picker." error="Required">
        <Input id="alias" />
      </FormField>,
    )

    const ids = screen.getByLabelText('Alias').getAttribute('aria-describedby')!.split(' ')
    expect(ids).toHaveLength(2)
    const text = ids.map((i) => document.getElementById(i)?.textContent).join(' ')
    expect(text).toContain('Shown in the picker.')
    expect(text).toContain('Required')
  })

  it('keeps a caller’s own aria-describedby alongside the generated one', () => {
    render(
      <FormField label="Alias" htmlFor="alias" error="Required">
        <Input id="alias" aria-describedby="extra-note" />
      </FormField>,
    )

    const ids = screen.getByLabelText('Alias').getAttribute('aria-describedby')!.split(' ')
    expect(ids).toContain('extra-note')
    expect(ids.length).toBeGreaterThan(1)
    expect(document.getElementById(ids.find((i) => i !== 'extra-note')!)).toHaveTextContent(
      'Required',
    )
  })

  // Cloning a fragment would accept the props and drop them, so the wiring must not claim to work.
  it('leaves a fragment child alone rather than wiring nothing', () => {
    render(
      <FormField label="Alias" htmlFor="alias" error="Required">
        <>
          <Input id="alias" />
        </>
      </FormField>,
    )

    expect(screen.getByLabelText('Alias')).not.toHaveAttribute('aria-invalid')
    expect(screen.getByRole('alert')).toHaveTextContent('Required')
  })

  // A remount loses focus mid-correction, and 'onChange' revalidation makes that the normal path.
  it('keeps the same DOM node when the error appears and clears', () => {
    const { rerender } = render(
      <FormField label="Name" htmlFor="name">
        <Input id="name" />
      </FormField>,
    )
    // A marker survives an update and dies with a remount.
    screen.getByLabelText('Name').setAttribute('data-probe', 'yes')

    rerender(
      <FormField label="Name" htmlFor="name" error="Required">
        <Input id="name" />
      </FormField>,
    )
    expect(screen.getByLabelText('Name')).toHaveAttribute('data-probe', 'yes')

    rerender(
      <FormField label="Name" htmlFor="name">
        <Input id="name" />
      </FormField>,
    )
    expect(screen.getByLabelText('Name')).toHaveAttribute('data-probe', 'yes')
  })

  it('keeps focus and the caret while the error clears under the cursor', async () => {
    const user = userEvent.setup()
    const { rerender } = render(
      <FormField label="Name" htmlFor="name" error="Required">
        <Input id="name" />
      </FormField>,
    )

    await user.click(screen.getByLabelText('Name'))
    await user.keyboard('a')
    expect(screen.getByLabelText('Name')).toHaveFocus()

    // What react-hook-form does on the next keystroke once the field validates.
    rerender(
      <FormField label="Name" htmlFor="name">
        <Input id="name" />
      </FormField>,
    )

    expect(screen.getByLabelText('Name')).toHaveFocus()
    expect(screen.getByLabelText('Name')).toHaveValue('a')
  })

  it('does not overrule a caller that set aria-invalid deliberately', () => {
    render(
      <FormField label="Alias" htmlFor="alias" error="Required">
        <Input id="alias" aria-invalid={false} />
      </FormField>,
    )

    expect(screen.getByLabelText('Alias')).toHaveAttribute('aria-invalid', 'false')
  })

  it('honours a caller-supplied descriptionId so other copy can reference it', () => {
    render(
      <FormField
        label="Alias"
        htmlFor="alias"
        description="Shown in the picker."
        descriptionId="alias-hint"
      >
        <Input id="alias" />
      </FormField>,
    )

    expect(document.getElementById('alias-hint')).toHaveTextContent('Shown in the picker.')
    expect(screen.getByLabelText('Alias')).toHaveAttribute('aria-describedby', 'alias-hint')
  })

  it('leaves multi-element children alone rather than guessing which is the control', () => {
    render(
      <FormField label="Range" error="Required">
        <Input aria-label="From" />
        <Input aria-label="To" />
      </FormField>,
    )

    // Nothing to clone unambiguously, so neither input is annotated - but the error still shows.
    expect(screen.getByLabelText('From')).not.toHaveAttribute('aria-invalid')
    expect(screen.getByRole('alert')).toHaveTextContent('Required')
  })
})
