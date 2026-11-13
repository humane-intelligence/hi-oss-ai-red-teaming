import { describe, expect, it } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { SidebarNav } from './sidebar-nav'
import { authWrapper } from '@/lib/auth/auth.testutils'

function renderAt(path: string) {
  const Wrapper = authWrapper(['evaluation_groups:read', 'evaluations:read', 'conversations:read'])
  render(
    <Wrapper>
      <MemoryRouter initialEntries={[path]}>
        <SidebarNav />
      </MemoryRouter>
    </Wrapper>,
  )
}

function current() {
  return within(screen.getByRole('navigation'))
    .getAllByRole('link')
    .filter((l) => l.getAttribute('aria-current') === 'page')
    .map((l) => l.textContent)
}

describe('SidebarNav — active item', () => {
  it.each([
    '/evaluations/e1/conversations/c1',
    '/evaluations/e1/conversation-groups/g1',
    '/evaluations/e1/conversation-groups/new',
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
