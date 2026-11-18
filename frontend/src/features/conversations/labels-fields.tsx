import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'
import type { EvaluationLabelCategoryResponse } from '@/lib/api/types'
import { BARE, type LabelValues } from './label-values'

const same = (a: string, b: string) => a.toLowerCase() === b.toLowerCase()

// One checkbox per tag, grouped by category and read as "category: tag"; a category without tags is
// a single checkbox for the category itself. `held` is what the group stores now: a tag since
// dropped from its category's list, or text from before categories without tags became markers,
// stays on offer so editing another tag doesn't silently clear it.
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
  const toggle = (categoryId: string, tag: string, on: boolean) => {
    const rest = (value[categoryId] ?? []).filter((v) => !same(v, tag))
    onChange({ ...value, [categoryId]: on ? [...rest, tag] : rest })
  }

  return (
    <div className="space-y-3">
      {categories.map((category) => {
        const picked = value[category.id] ?? []
        const unlisted = (held[category.id] ?? []).filter(
          (v) => v !== BARE && !category.values.some((listed) => same(listed, v)),
        )
        const options = [
          ...(category.values.length > 0
            ? category.values.map((tag) => ({ tag, text: `${category.name}: ${tag}` }))
            : [{ tag: BARE, text: category.name }]),
          ...unlisted.map((tag) => ({ tag, text: `${category.name}: ${tag} (not in the list)` })),
        ]
        return (
          <fieldset key={category.id} className="space-y-1.5">
            <legend className="sr-only">{category.name}</legend>
            {options.map(({ tag, text }, i) => {
              const id = `${idPrefix}-${category.id}-${i}`
              return (
                <div key={tag} className="flex items-center gap-2">
                  <Checkbox
                    id={id}
                    checked={picked.some((v) => same(v, tag))}
                    onCheckedChange={(next) => toggle(category.id, tag, next === true)}
                  />
                  <Label htmlFor={id} className="font-normal break-words">
                    {text}
                  </Label>
                </div>
              )
            })}
          </fieldset>
        )
      })}
    </div>
  )
}
