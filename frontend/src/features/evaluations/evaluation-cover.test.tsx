import { describe, expect, it } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { EvaluationCover } from './evaluation-cover'

describe('EvaluationCover', () => {
  it('shows the cover image for a key', () => {
    render(<EvaluationCover imageKey="covers/a.png" alt="Medication advice cover" />)
    const img = screen.getByRole('img', { name: 'Medication advice cover' })
    expect(img).toHaveAttribute('src', expect.stringContaining('/api/v1/images/covers/a.png'))
  })

  it('shows a placeholder without a key', () => {
    render(<EvaluationCover imageKey={null} alt="Medication advice cover" />)
    expect(screen.getByRole('img', { name: 'No cover image' })).toBeInTheDocument()
    expect(screen.queryByRole('img', { name: 'Medication advice cover' })).not.toBeInTheDocument()
  })

  it('falls back to the placeholder when the image fails to load', () => {
    render(<EvaluationCover imageKey="covers/gone.png" alt="Medication advice cover" />)
    fireEvent.error(screen.getByRole('img', { name: 'Medication advice cover' }))
    expect(screen.getByRole('img', { name: 'No cover image' })).toBeInTheDocument()
  })
})
