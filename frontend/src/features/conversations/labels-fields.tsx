import { FormField } from '@/components/shared/form-field'
import { Input } from '@/components/ui/input'
import { PlainSelect } from '@/components/ui/plain-select'
import { MAX_GROUP_LABEL_VALUE_LEN } from '@/lib/api/limits'
import type { EvaluationLabelCategoryResponse } from '@/lib/api/types'
import type { LabelValues } from './label-values'

// One field per category: a select for a category with listed values, a text input for free text.
// `held` is what the group stores now: a value since dropped from its category's list stays an
// option, so editing another label doesn't silently clear it.
export function LabelsFields({
  categories,
  value,
  onChange,
  held = {},
  idPrefix,
}: {
  categories: EvaluationLabelCategoryResponse[]
  value: LabelValues
  onChange: (next: LabelValues) => void
  held?: LabelValues
  idPrefix: string
}) {
  const set = (categoryId: string, v: string) => onChange({ ...value, [categoryId]: v })

  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {categories.map((category) => {
        const id = `${idPrefix}-${category.id}`
        const current = value[category.id] ?? ''
        const kept = held[category.id]
        const keptUnlisted =
          kept && !category.values.some((v) => v.toLowerCase() === kept.toLowerCase())
        return (
          <FormField key={category.id} label={category.name} htmlFor={id}>
            {category.values.length > 0 ? (
              <PlainSelect
                id={id}
                value={current}
                onChange={(e) => set(category.id, e.target.value)}
              >
                <option value="">— none —</option>
                {category.values.map((v) => (
                  <option key={v} value={v}>
                    {v}
                  </option>
                ))}
                {keptUnlisted && <option value={kept}>{kept} (no longer listed)</option>}
              </PlainSelect>
            ) : (
              <Input
                id={id}
                value={current}
                maxLength={MAX_GROUP_LABEL_VALUE_LEN}
                onChange={(e) => set(category.id, e.target.value)}
              />
            )}
          </FormField>
        )
      })}
    </div>
  )
}
