// Ids key off the tab value, which is unique inside a page's tab vocabulary — this assumes one
// tablist per page, which is what every call site has.
//
// Its own module rather than a second export from `tabs.tsx`: `react-refresh/only-export-components`
// fails a component file that also exports a function (same reason `chart-day.ts` is split out).
export const tabId = (value: string) => `tab-${value}`
export const tabPanelId = (value: string) => `tabpanel-${value}`

// The pairing a tab needs on the other end. The page owns its panels — it decides what is inside
// and when they mount — so the ARIA half is handed back rather than owned by `Tabs`. `tabIndex`
// makes the panel itself reachable, so a keyboard user leaving the tablist lands in the content it
// controls even on a panel whose first render carries no focusable element.
export function tabPanelProps(value: string) {
  return {
    id: tabPanelId(value),
    role: 'tabpanel' as const,
    'aria-labelledby': tabId(value),
    tabIndex: 0,
  }
}
