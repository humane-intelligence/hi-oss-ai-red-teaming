import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { BulkResultDialog, BulkResultSummary, type BulkOutcome } from './bulk-result-dialog'

const OUTCOME: BulkOutcome = {
  total: 3,
  succeeded: 1,
  failed: 2,
  results: [
    { row_key: 'u1', status: 'ok', error: null },
    {
      row_key: 'u2',
      status: 'failed',
      error: { title: 'Conflict', detail: 'Account is still onboarding.' },
    },
    { row_key: 'u3', status: 'failed', error: { title: 'Not Found' } },
  ],
}

const LABELS: Record<string, string> = { u1: 'ann@example.com', u2: 'bob@example.com' }
const labelFor = (key: string) => LABELS[key] ?? key

describe('BulkResultSummary', () => {
  it('counts the outcome and lists only the failed rows, labelled by row_key', () => {
    render(<BulkResultSummary result={OUTCOME} labelFor={labelFor} />)

    expect(screen.getByText(/1 of 3 succeeded, 2 failed/i)).toBeInTheDocument()
    const rows = screen.getAllByRole('listitem')
    expect(rows).toHaveLength(2)
    expect(rows[0]).toHaveTextContent('bob@example.com: Account is still onboarding.')
    // Detail beats the generic title; a row with no label falls back to its key.
    expect(rows[0]).not.toHaveTextContent('Conflict')
    expect(rows[1]).toHaveTextContent('u3: Not Found')
  })

  it('renders no list when every row succeeded', () => {
    render(
      <BulkResultSummary
        result={{ total: 2, succeeded: 2, failed: 0, results: [{ row_key: 'u1', status: 'ok' }] }}
        labelFor={labelFor}
      />,
    )

    expect(screen.getByText(/2 of 2 succeeded\./i)).toBeInTheDocument()
    expect(screen.queryByRole('listitem')).not.toBeInTheDocument()
  })
})

describe('BulkResultDialog', () => {
  it('shows the summary under its title and closes on Close', async () => {
    const onOpenChange = vi.fn()
    render(
      <BulkResultDialog
        open
        onOpenChange={onOpenChange}
        title="Password reset links"
        result={OUTCOME}
        labelFor={labelFor}
      />,
    )

    expect(screen.getByRole('heading', { name: 'Password reset links' })).toBeInTheDocument()
    expect(screen.getByText(/1 of 3 succeeded/i)).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })
})
