import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { SearchForm } from './search-form'

function Harness({ onSubmit }: { onSubmit: (v: string) => void }) {
  const [value, setValue] = useState('')
  return (
    <SearchForm
      value={value}
      onChange={setValue}
      onSubmit={() => onSubmit(value)}
      placeholder="Search…"
      label="Search things"
    />
  )
}

describe('SearchForm', () => {
  it('submits from the icon button and from Enter', async () => {
    const onSubmit = vi.fn()
    const user = userEvent.setup()
    render(<Harness onSubmit={onSubmit} />)

    await user.type(screen.getByRole('textbox', { name: 'Search things' }), 'abc')
    await user.click(screen.getByRole('button', { name: 'Search' }))
    await user.type(screen.getByRole('textbox', { name: 'Search things' }), 'd{Enter}')

    expect(onSubmit.mock.calls).toEqual([['abc'], ['abcd']])
    expect(screen.getByRole('search')).toBeInTheDocument()
  })
})
