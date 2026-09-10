import type { UseFormRegisterReturn } from 'react-hook-form'
import { Info } from 'lucide-react'
import { useLicenses } from './queries'
import { findNoLicense } from './no-license'
import { FormField } from '@/components/shared/form-field'
import { PlainSelect } from '@/components/ui/plain-select'
import type { DataLicenseSummary } from '@/lib/api/types'

// The marker rides the option text: a native <select> renders no markup per option, and this is
// where the encryption decision is actually taken. Same word as the badge on the list and detail
// pages.
const PROTECTED_MARKER = ' · protected'

// The data-license selector shared by the evaluation, evaluation-group, and system-preferences
// forms. Owns the catalog fetch, the option list, the orphan-id fallback (a selected licence no
// longer in the catalog stays selectable), and a description line showing the selected licence's
// `short_description` so the operator sees what it covers, not just its name. The empty option is
// the "inherit" sentinel: `inheritLabel` is its prefix ("Inherit" / "Platform default") and
// `inheritLicense` the licence it resolves to; when `inheritLabel` is omitted the sentinel is not
// rendered at all — the platform-settings page IS the root of the cascade, so there is nothing to
// inherit from and the selection must be a concrete licence. The catalog's "no license" entry is
// **opt-in** via `offerNoLicense`, mirroring `inheritLabel`: a form that does not name it does not get
// it, so a new picker cannot start offering "no licence" where the product does not want it — and
// outside platform-settings such a write would succeed silently.
export function LicensePicker({
  field,
  value,
  error,
  inheritLabel,
  inheritLicense,
  offerNoLicense = false,
  describedBy,
  id = 'data_license_id',
}: {
  field: UseFormRegisterReturn
  value: string
  error?: string
  inheritLabel?: string
  inheritLicense?: DataLicenseSummary | null
  offerNoLicense?: boolean
  // Id of a note the form renders below the picker, folded into the select's `aria-describedby` —
  // a hint that only sits next to the control is invisible to a screen reader.
  describedBy?: string
  id?: string
}) {
  const licenses = useLicenses()
  const catalog = licenses.data?.items ?? []
  const noLicenseId = findNoLicense(catalog)?.id
  // A withheld entry is still rendered when it is the current value: a controlled `<select>` whose
  // value matches no option displays the FIRST one, so a group carrying a licence this form does not
  // offer would read as whatever sorts first — "no licence" showing up as "Platform default".
  const items = offerNoLicense
    ? catalog
    : catalog.filter((l) => l.id !== noLicenseId || l.id === value)
  const platformDefault = items.find((l) => l.is_default)
  const inheritTarget = inheritLicense ?? platformDefault
  const inheritDisplay = inheritTarget?.spdx_id ?? inheritTarget?.name
  // Settled, not successful: with the catalog unavailable `items` is empty, and a controlled
  // `<select>` whose value matches no option displays the FIRST one — so without this the field
  // would read as "Platform default" while the form still holds (and would re-send) the stored id.
  const isOrphan = !licenses.isPending && value !== '' && !items.some((l) => l.id === value)
  // The licence the current selection resolves to — the inherit target for the empty sentinel,
  // else the chosen row (an orphan id resolves to nothing, so no description shows).
  const selected = value === '' ? inheritTarget : items.find((l) => l.id === value)
  const hint = selected?.short_description
  const hintId = `${id}-hint`

  return (
    <FormField label="Data license" htmlFor={id} error={error}>
      {/* Controlled on the form's value: an uncontrolled select drops a preselected id whose
          option has not loaded yet, and nothing re-applies it once the list arrives. */}
      <PlainSelect
        id={id}
        {...field}
        value={value}
        aria-describedby={
          [hint ? hintId : null, describedBy].filter(Boolean).join(' ') || undefined
        }
      >
        {inheritLabel !== undefined && (
          <option value="" title={inheritTarget?.short_description ?? undefined}>
            {inheritLabel}
            {inheritDisplay ? ` (${inheritDisplay})` : ''}
            {inheritTarget?.protects_conversation_data ? PROTECTED_MARKER : ''}
          </option>
        )}
        {isOrphan && <option value={value}>(referenced license, not listed)</option>}
        {items.map((l) => (
          <option key={l.id} value={l.id} title={l.short_description ?? undefined}>
            {l.name}
            {l.protects_conversation_data ? PROTECTED_MARKER : ''}
          </option>
        ))}
      </PlainSelect>
      {hint && (
        <p id={hintId} className="text-muted-foreground flex items-start gap-1.5 text-xs">
          <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          <span>{hint}</span>
        </p>
      )}
    </FormField>
  )
}
