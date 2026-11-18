// API enum values are snake_case; the console shows them as "Pending approval".
export function sentenceCase(value: string) {
  const words = value.replace(/_/g, ' ')
  return words.charAt(0).toUpperCase() + words.slice(1)
}
