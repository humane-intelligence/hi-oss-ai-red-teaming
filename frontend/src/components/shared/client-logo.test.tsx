import { describe, expect, it } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { ClientLogo } from './client-logo'

describe('ClientLogo', () => {
  it('shows the fallback once the image fails to load', () => {
    render(<ClientLogo light="a.png" dark="b.png" alt="Home" fallback={<span>Brand</span>} />)

    fireEvent.error(screen.getAllByRole('img', { name: 'Home' })[0]!)

    expect(screen.queryByRole('img', { name: 'Home' })).toBeNull()
    expect(screen.getByText('Brand')).toBeInTheDocument()
  })
})
