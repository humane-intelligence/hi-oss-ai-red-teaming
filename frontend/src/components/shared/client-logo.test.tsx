import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { publicImageUrl } from '@/lib/api/images'
import { ClientLogo } from './client-logo'

const imgs = () => screen.queryAllByRole('img', { name: 'Home' })

describe('ClientLogo', () => {
  it('lets a variant that fails borrow the other one', () => {
    render(<ClientLogo light="a.png" dark="b.png" alt="Home" fallback={<span>Brand</span>} />)

    fireEvent.error(imgs()[1]!)

    expect(imgs().map((img) => img.getAttribute('src'))).toEqual([
      publicImageUrl('a.png'),
      publicImageUrl('a.png'),
    ])
    expect(screen.queryByText('Brand')).toBeNull()
  })

  it('shows the fallback once both variants fail to load', () => {
    render(<ClientLogo light="a.png" dark="b.png" alt="Home" fallback={<span>Brand</span>} />)

    fireEvent.error(imgs()[1]!)
    fireEvent.error(imgs()[0]!)

    expect(imgs()).toHaveLength(0)
    expect(screen.getByText('Brand')).toBeInTheDocument()
  })

  it('reports ready once both slots have loaded', () => {
    const onReady = vi.fn()
    render(<ClientLogo light="a.png" dark="b.png" alt="Home" onReady={onReady} />)

    fireEvent.load(imgs()[0]!)
    expect(onReady).not.toHaveBeenCalled()
    fireEvent.load(imgs()[1]!)

    expect(onReady).toHaveBeenCalled()
  })

  it('reports ready when a failed slot borrows an image that already loaded', () => {
    const onReady = vi.fn()
    render(<ClientLogo light="a.png" dark="b.png" alt="Home" onReady={onReady} />)

    fireEvent.load(imgs()[0]!)
    fireEvent.error(imgs()[1]!)

    expect(onReady).toHaveBeenCalled()
  })

  it('reports ready when both fail and the fallback shows', () => {
    const onReady = vi.fn()
    render(<ClientLogo light="a.png" dark="b.png" alt="Home" onReady={onReady} />)

    fireEvent.error(imgs()[1]!)
    fireEvent.error(imgs()[0]!)

    expect(onReady).toHaveBeenCalled()
  })
})
