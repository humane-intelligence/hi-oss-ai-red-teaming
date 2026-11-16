// "Oct 28, 2026". A bare `YYYY-MM-DD` is a calendar date: `new Date` would read it as UTC midnight
// and show the day before anywhere west of UTC, so it is read as local midnight instead.
export function formatDate(value: string) {
  const date = /^\d{4}-\d{2}-\d{2}$/.test(value) ? new Date(`${value}T00:00:00`) : new Date(value)
  return new Intl.DateTimeFormat('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  }).format(date)
}
