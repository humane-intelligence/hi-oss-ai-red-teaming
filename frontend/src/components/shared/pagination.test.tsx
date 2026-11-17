import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Pagination } from './pagination'

describe('Pagination', () => {
  it('steps back to the last page when the offset is past the end', () => {
    const onOffsetChange = vi.fn()
    render(<Pagination offset={20} limit={20} total={20} onOffsetChange={onOffsetChange} />)

    expect(onOffsetChange).toHaveBeenCalledExactlyOnceWith(0)
    expect(screen.getByText('1–20 of 20')).toBeInTheDocument()
  })

  it('lands on the last page that still has rows, not the first', () => {
    const onOffsetChange = vi.fn()
    render(<Pagination offset={60} limit={20} total={45} onOffsetChange={onOffsetChange} />)

    expect(onOffsetChange).toHaveBeenCalledExactlyOnceWith(40)
  })

  it('goes back to the start when nothing is left', () => {
    const onOffsetChange = vi.fn()
    render(<Pagination offset={20} limit={20} total={0} onOffsetChange={onOffsetChange} />)

    expect(onOffsetChange).toHaveBeenCalledExactlyOnceWith(0)
    expect(screen.getByText('0–0 of 0')).toBeInTheDocument()
  })

  it('reports the step back through onOffsetCorrect when a list gives one', () => {
    const onOffsetChange = vi.fn()
    const onOffsetCorrect = vi.fn()
    render(
      <Pagination
        offset={20}
        limit={20}
        total={20}
        onOffsetChange={onOffsetChange}
        onOffsetCorrect={onOffsetCorrect}
      />,
    )

    expect(onOffsetCorrect).toHaveBeenCalledExactlyOnceWith(0)
    expect(onOffsetChange).not.toHaveBeenCalled()
  })

  it('leaves an offset inside the list alone', () => {
    const onOffsetChange = vi.fn()
    render(<Pagination offset={20} limit={20} total={21} onOffsetChange={onOffsetChange} />)

    expect(onOffsetChange).not.toHaveBeenCalled()
    expect(screen.getByText('21–21 of 21')).toBeInTheDocument()
  })
})
