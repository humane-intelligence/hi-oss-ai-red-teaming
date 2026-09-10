import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { Breadcrumbs } from './breadcrumbs'

describe('Breadcrumbs', () => {
  it('renders all items', () => {
    render(
      <MemoryRouter>
        <Breadcrumbs
          items={[
            { label: 'Home', to: '/' },
            { label: 'Groups', to: '/groups' },
            { label: 'Current' },
          ]}
        />
      </MemoryRouter>,
    )
    expect(screen.getByText('Home')).toBeInTheDocument()
    expect(screen.getByText('Groups')).toBeInTheDocument()
    expect(screen.getByText('Current')).toBeInTheDocument()
  })

  it('marks a linked leaf as the current page only when it points at this page', () => {
    // The regression this guards: binding `aria-current` to the leaf's *position* marks the group
    // crumb on every child page, where that link is the way out rather than where you are.
    render(
      <MemoryRouter initialEntries={['/evaluation-groups/g1/evaluations/e1']}>
        <Breadcrumbs
          items={[
            { label: 'Groups', to: '/evaluation-groups' },
            { label: 'Spring Sprint', to: '/evaluation-groups/g1' },
          ]}
        />
      </MemoryRouter>,
    )
    expect(screen.getByRole('link', { name: 'Spring Sprint' })).not.toHaveAttribute('aria-current')
    expect(screen.getByRole('link', { name: 'Groups' })).not.toHaveAttribute('aria-current')
  })

  it('marks a linked leaf as the current page when the link is this page', () => {
    render(
      <MemoryRouter initialEntries={['/evaluation-groups/g1']}>
        <Breadcrumbs
          items={[
            { label: 'Groups', to: '/evaluation-groups' },
            { label: 'Spring Sprint', to: '/evaluation-groups/g1' },
          ]}
        />
      </MemoryRouter>,
    )
    expect(screen.getByRole('link', { name: 'Spring Sprint' })).toHaveAttribute(
      'aria-current',
      'page',
    )
  })

  it('items with `to` are rendered as links', () => {
    render(
      <MemoryRouter>
        <Breadcrumbs
          items={[
            { label: 'Home', to: '/' },
            { label: 'Groups', to: '/groups' },
            { label: 'Current' },
          ]}
        />
      </MemoryRouter>,
    )
    expect(screen.getByRole('link', { name: 'Home' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Groups' })).toBeInTheDocument()
  })

  it('last item without `to` is plain text, not a link', () => {
    render(
      <MemoryRouter>
        <Breadcrumbs items={[{ label: 'Home', to: '/' }, { label: 'Current' }]} />
      </MemoryRouter>,
    )
    expect(screen.queryByRole('link', { name: 'Current' })).toBeNull()
    expect(screen.getByText('Current')).toBeInTheDocument()
  })

  it('exposes the trail as a list, so its depth is announced', () => {
    render(
      <MemoryRouter>
        <Breadcrumbs items={[{ label: 'Home', to: '/' }, { label: 'Current' }]} />
      </MemoryRouter>,
    )
    // The list itself, not just the items: jsdom reports `listitem` for an orphaned `<li>`, so
    // asserting the items alone would survive the wrapper being dropped.
    expect(screen.getByRole('list')).toBeInTheDocument()
    // Two entries; the decorative separator is aria-hidden and so not an item.
    expect(screen.getAllByRole('listitem')).toHaveLength(2)
  })

  it('marks the leaf as the current page', () => {
    render(
      <MemoryRouter>
        <Breadcrumbs
          items={[
            { label: 'Home', to: '/' },
            { label: 'Groups', to: '/groups' },
            { label: 'Current' },
          ]}
        />
      </MemoryRouter>,
    )
    expect(screen.getByText('Current')).toHaveAttribute('aria-current', 'page')
    // Only the leaf: an ancestor you can still navigate to is not the page you are on.
    expect(screen.getByRole('link', { name: 'Groups' })).not.toHaveAttribute('aria-current')
  })

  it('takes its accessible name from the label, so two trails on one page stay distinguishable', () => {
    render(
      <MemoryRouter>
        <Breadcrumbs
          items={[{ label: 'Evaluation Groups', to: '/evaluation-groups' }]}
          label="Evaluation group"
        />
        <Breadcrumbs items={[{ label: 'Prompt injection' }]} />
      </MemoryRouter>,
    )
    expect(screen.getByRole('navigation', { name: 'Evaluation group' })).toBeInTheDocument()
    expect(screen.getByRole('navigation', { name: 'Breadcrumb' })).toBeInTheDocument()
  })
})
