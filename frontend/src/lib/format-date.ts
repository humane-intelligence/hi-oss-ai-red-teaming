const format = (date: Date) =>
  new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', year: 'numeric' }).format(date)

// A `YYYY-MM-DD` field: `new Date` would read it as UTC midnight and show the day before anywhere
// west of UTC, so it is read as local midnight.
export function formatCalendarDate(value: string) {
  return format(new Date(`${value}T00:00:00`))
}

export function formatTimestamp(value: string) {
  return format(new Date(value))
}
