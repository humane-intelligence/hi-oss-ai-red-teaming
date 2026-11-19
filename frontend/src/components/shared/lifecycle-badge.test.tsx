import { sentenceCase } from '@/lib/sentence-case'
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { LifecycleBadge } from './lifecycle-badge'

describe('LifecycleBadge', () => {
  it.each([
    ['draft', 'bg-muted'],
    ['pending_approval', 'bg-warn-surface'],
    ['under_review', 'bg-warn-surface'],
    ['changes_requested', 'bg-changes-surface'],
    ['approved', 'bg-approved-surface'],
    ['published', 'bg-ok-surface'],
    ['not_approved', 'bg-err-surface'],
    ['rejected', 'bg-err-surface'],
    ['inactive', 'bg-muted/60'],
    ['completed', 'bg-muted/60'],
    ['new', 'border-dashed'],
  ] as const)('gives %s its stage tone', (status, cls) => {
    render(<LifecycleBadge status={status} />)
    expect(screen.getByText(sentenceCase(status))).toHaveClass(cls)
  })

  it('marks only a published stage with a dot', () => {
    const { rerender } = render(<LifecycleBadge status="published" />)
    expect(screen.getByTestId('published-dot')).toBeInTheDocument()
    rerender(<LifecycleBadge status="approved" />)
    expect(screen.queryByTestId('published-dot')).toBeNull()
  })
})
