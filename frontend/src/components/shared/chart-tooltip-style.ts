// Recharts hardcodes a white tooltip surface with a #ccc border and lets the label inherit the page's
// text colour. Measured in the dark theme: label rgb(233,241,239) on rgb(255,255,255) — white on
// white — and the value line at 2.93:1, under the 4.5:1 minimum for text. Both charts therefore pass
// the app's own popover tokens instead of Recharts' defaults.
export const TOOLTIP_CONTENT_STYLE = {
  backgroundColor: 'var(--color-popover)',
  border: '1px solid var(--color-border)',
  borderRadius: '0.5rem',
}

export const TOOLTIP_LABEL_STYLE = { color: 'var(--color-popover-foreground)' }

export const TOOLTIP_ITEM_STYLE = { color: 'var(--color-popover-foreground)' }
