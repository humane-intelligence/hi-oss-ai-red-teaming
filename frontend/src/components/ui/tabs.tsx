import { useRef, type KeyboardEvent, type ReactNode } from 'react'
import { cn } from '@/lib/utils'
import { tabId, tabPanelId } from './tab-panel'

export type TabItem = { value: string; label: ReactNode }

export function Tabs({
  tabs,
  value,
  onChange,
  className,
}: {
  tabs: TabItem[]
  value: string
  onChange: (v: string) => void
  className?: string
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([])

  // Arrows move focus, they do not select: `onChange` pushes a history entry, so activating on every
  // arrow key would bury the previous page under one entry per tab passed over. Enter and Space
  // activate through the button's own click handling.
  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    // The handler sits on the tablist, so the event always comes from one of the buttons.
    const index = refs.current.findIndex((node) => node === document.activeElement)
    const target = {
      ArrowRight: (index + 1) % tabs.length,
      ArrowLeft: (index - 1 + tabs.length) % tabs.length,
      Home: 0,
      End: tabs.length - 1,
    }[event.key]
    if (target === undefined) return
    event.preventDefault()
    refs.current[target]?.focus()
  }

  return (
    <div role="tablist" onKeyDown={onKeyDown} className={cn('flex gap-1 border-b', className)}>
      {tabs.map((tab, i) => {
        const selected = value === tab.value
        return (
          <button
            key={tab.value}
            ref={(node) => {
              refs.current[i] = node
            }}
            type="button"
            role="tab"
            id={tabId(tab.value)}
            // Only the open panel exists in the DOM, so an inactive tab has nothing to point at.
            aria-controls={selected ? tabPanelId(tab.value) : undefined}
            aria-selected={selected}
            // Roving tabindex: the tablist is one stop in the tab sequence, and arrows move within it.
            tabIndex={selected ? 0 : -1}
            onClick={() => onChange(tab.value)}
            className={cn(
              'border-b-2 px-3 py-2 text-sm font-medium transition-colors',
              selected
                ? 'border-primary text-foreground'
                : 'text-muted-foreground hover:text-foreground border-transparent',
            )}
          >
            {tab.label}
          </button>
        )
      })}
    </div>
  )
}
