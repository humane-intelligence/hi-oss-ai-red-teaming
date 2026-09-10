import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ColumnsMenu } from './columns-menu'

const columns = [
  { id: 'title', label: 'Title' },
  { id: 'status', label: 'Status' },
  { id: 'created_at', label: 'Created' },
]

describe('ColumnsMenu', () => {
  it('lists a checkbox per column, checked when visible', async () => {
    const user = userEvent.setup()
    render(<ColumnsMenu columns={columns} hiddenColumns={['status']} onChange={vi.fn()} />)
    await user.click(screen.getByRole('button', { name: 'Toggle columns' }))

    expect((screen.getByLabelText('Title') as HTMLInputElement).checked).toBe(true)
    expect((screen.getByLabelText('Status') as HTMLInputElement).checked).toBe(false)
    expect((screen.getByLabelText('Created') as HTMLInputElement).checked).toBe(true)
  })

  it('hides a visible column by adding its id to hiddenColumns', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<ColumnsMenu columns={columns} hiddenColumns={[]} onChange={onChange} />)
    await user.click(screen.getByRole('button', { name: 'Toggle columns' }))
    await user.click(screen.getByLabelText('Title'))
    expect(onChange).toHaveBeenCalledWith(['title'])
  })

  it('shows a hidden column by removing its id', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(
      <ColumnsMenu columns={columns} hiddenColumns={['title', 'status']} onChange={onChange} />,
    )
    await user.click(screen.getByRole('button', { name: 'Toggle columns' }))
    await user.click(screen.getByLabelText('Title'))
    expect(onChange).toHaveBeenCalledWith(['status'])
  })
})
