import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Markdown } from './markdown'

describe('Markdown', () => {
  it('renders bold and lists', () => {
    render(<Markdown content={'**bold** and\n\n- one\n- two'} />)
    expect(screen.getByText('bold', { selector: 'strong' })).toBeInTheDocument()
    expect(screen.getAllByRole('listitem')).toHaveLength(2)
  })

  it('renders fenced code', () => {
    render(<Markdown content={'```\nconst x = 1\n```'} />)
    expect(screen.getByText(/const x = 1/)).toBeInTheDocument()
  })

  it('does NOT execute embedded HTML (escapes it as text)', () => {
    const { container } = render(<Markdown content={'<script>alert(1)</script> hi'} />)
    expect(container.querySelector('script')).toBeNull()
  })

  it('opens links in a new tab safely', () => {
    render(<Markdown content={'[x](https://example.com)'} />)
    const link = screen.getByRole('link', { name: 'x' })
    expect(link).toHaveAttribute('rel', expect.stringContaining('noopener'))
  })
})
