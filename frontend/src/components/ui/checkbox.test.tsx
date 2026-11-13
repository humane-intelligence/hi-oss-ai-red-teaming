import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Checkbox } from './checkbox'

describe('Checkbox', () => {
  it('renders a mixed state with its own indicator', () => {
    render(<Checkbox checked="indeterminate" aria-label="Task" />)

    const box = screen.getByRole('checkbox', { name: 'Task' })
    expect(box).toBePartiallyChecked()
    expect(box).toHaveClass('data-[state=indeterminate]:bg-primary')
    expect(box.querySelector('.lucide-minus')).not.toBeNull()
    expect(box.querySelector('.lucide-check')).toBeNull()
  })

  it('renders the check indicator when checked', () => {
    render(<Checkbox checked aria-label="Task" />)

    const box = screen.getByRole('checkbox', { name: 'Task' })
    expect(box).toBeChecked()
    expect(box.querySelector('.lucide-check')).not.toBeNull()
  })
})
