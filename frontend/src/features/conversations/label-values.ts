// category id → value; a blank value means the category is unset.
export type LabelValues = Record<string, string>

// The map a create or a PATCH sends: blanks dropped on create, kept on a PATCH so they clear.
export function labelPayload(value: LabelValues, { keepBlanks }: { keepBlanks: boolean }) {
  return Object.fromEntries(
    Object.entries(value)
      .map(([k, v]) => [k, v.trim()] as const)
      .filter(([, v]) => keepBlanks || v !== ''),
  )
}
