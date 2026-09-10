import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MessageMeta } from './message-meta'

describe('MessageMeta', () => {
  it('renders nothing without extra', () => {
    const { container } = render(<MessageMeta extra={null} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders finish_reason and total token usage', () => {
    render(<MessageMeta extra={{ finish_reason: 'stop', usage: { total_tokens: 46 } }} />)
    expect(screen.getByText(/finish: stop · 46 tok/i)).toBeInTheDocument()
  })

  it('shows a finish reason alone when usage is absent', () => {
    render(<MessageMeta extra={{ finish_reason: 'length' }} />)
    expect(screen.getByText(/finish: length/i)).toBeInTheDocument()
  })

  it('surfaces an error marker', () => {
    render(<MessageMeta extra={{ error: 'rate limited' }} />)
    expect(screen.getByText(/rate limited/i)).toBeInTheDocument()
  })

  it('renders nothing when extra has no recognized fields', () => {
    const { container } = render(<MessageMeta extra={{ unrelated: 1 }} />)
    expect(container).toBeEmptyDOMElement()
  })
})
