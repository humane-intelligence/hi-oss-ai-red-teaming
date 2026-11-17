import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { SidebarNav } from './sidebar-nav'
import { authWrapper } from '@/lib/auth/auth.testutils'
import { server } from '@/test/msw/server'
import { publicSettingsHandler } from '@/test/msw/public-settings'

function renderAt(path: string) {
  const Wrapper = authWrapper(['evaluation_groups:read', 'evaluations:read', 'conversations:read'])
  render(
    <Wrapper>
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={[path]}>
          <SidebarNav />
        </MemoryRouter>
      </QueryClientProvider>
    </Wrapper>,
  )
}

// The whole document, not just <nav>: the logo link sits outside it and must never be marked.
function current() {
  return [...document.querySelectorAll('[aria-current="page"]')].map((l) => l.textContent)
}

describe('SidebarNav — active item', () => {
  it.each([
    '/evaluations/e1/conversations/c1',
    '/evaluations/e1/conversation-groups/g1',
    '/evaluations/e1/conversation-groups/new',
    // Routes match case-insensitively, so the menu must too.
    '/Evaluations/e1/Conversations/c1',
  ])('marks Conversations, not Evaluations, on %s', (path) => {
    renderAt(path)
    expect(current()).toEqual(['Conversations'])
  })

  it.each([
    ['/evaluations', 'Evaluations'],
    ['/evaluations/e1', 'Evaluations'],
    ['/evaluations/e1/edit', 'Evaluations'],
    ['/evaluation-groups/g1', 'Evaluation Groups'],
    ['/conversation-groups', 'Conversations'],
  ])('marks only the matching item on %s', (path, label) => {
    renderAt(path)
    expect(current()).toEqual([label])
  })

  it('marks Overview only on the root', () => {
    renderAt('/')
    expect(current()).toEqual(['Overview'])
  })
})

describe('SidebarNav — logo', () => {
  it('shows the default brand when the instance has no client logo', async () => {
    renderAt('/')
    expect(await screen.findByText('console')).toBeInTheDocument()
  })

  it('replaces the brand with the client logo, using the light one in both themes when it is the only one', async () => {
    server.use(publicSettingsHandler({ logo_light_key: '2026/11/17/light.png' }))
    renderAt('/')

    const logos = await screen.findAllByRole('img', { name: 'Home' })
    expect(logos.map((l) => l.getAttribute('src'))).toEqual([
      'http://localhost/api/v1/images/2026/11/17/light.png',
      'http://localhost/api/v1/images/2026/11/17/light.png',
    ])
    expect(screen.queryByText('console')).toBeNull()
  })
})
