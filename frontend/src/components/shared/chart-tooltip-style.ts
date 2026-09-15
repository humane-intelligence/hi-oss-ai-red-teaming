// Recharts hardcodes a white tooltip surface with a #ccc border and lets the label inherit the page's
// text colour. In the dark theme that puts the label at rgb(250,250,250) on rgb(255,255,255) — white
// on white. Both charts therefore pass the app's own popover tokens instead of Recharts' defaults.
export const TOOLTIP_CONTENT_STYLE = {
  backgroundColor: 'var(--color-popover)',
  border: '1px solid var(--color-border)',
  borderRadius: '0.5rem',
}

export const TOOLTIP_LABEL_STYLE = { color: 'var(--color-popover-foreground)' }

export const TOOLTIP_ITEM_STYLE = { color: 'var(--color-popover-foreground)' }
