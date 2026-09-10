import { z } from 'zod'
import type { InferenceParams } from '@/lib/api/types'

// Per-conversation inference knobs, shared by the create-conversation dialog and
// the multi-model group creator. Tuple: [key, label, input step, help text].
export const NUMERIC_PARAMS = [
  ['temperature', 'Temperature', 'any', 'Sampling randomness (0–2). Higher is more random.'],
  [
    'top_p',
    'Top P',
    'any',
    'Nucleus sampling: keep tokens up to this cumulative probability (0–1).',
  ],
  ['top_k', 'Top K', 'any', 'Sample only from the k most likely tokens.'],
  ['max_tokens', 'Max tokens', '1', 'Cap on the number of tokens generated.'],
  [
    'frequency_penalty',
    'Frequency penalty',
    'any',
    'Penalize tokens by how often they appear (−2 to 2).',
  ],
  [
    'presence_penalty',
    'Presence penalty',
    'any',
    'Penalize tokens that have appeared at all (−2 to 2).',
  ],
  [
    'repetition_penalty',
    'Repetition penalty',
    'any',
    'Penalize repeated tokens (Hugging Face style).',
  ],
  ['seed', 'Seed', '1', 'Fixed seed for reproducible sampling, where the provider supports it.'],
] as const

const numericField = z
  .string()
  .refine((v) => v === '' || !Number.isNaN(Number(v)), 'Must be a number')

// Zod shape for the AdvancedParams string-typed form fields, spread into each
// host form's own schema so the knob set is declared once.
export const paramsSchemaShape = {
  system_prompt: z.string(),
  temperature: numericField,
  top_p: numericField,
  top_k: numericField,
  max_tokens: numericField,
  frequency_penalty: numericField,
  presence_penalty: numericField,
  repetition_penalty: numericField,
  seed: numericField,
}

// System prompt is rendered separately from NUMERIC_PARAMS in each panel.
export const SYSTEM_PROMPT_HINT = 'Instructions prepended to the conversation to steer the model.'

// Shown once at the top of the open panel.
export const PARAMS_PANEL_HINT =
  'These change how the model generates responses. Clear a field to fall back to the inherited value shown under it.'

// Variant for the cascade root (the AI-model baseline): nothing upstream to inherit.
export const PARAMS_PANEL_HINT_BASELINE = 'These change how the model generates responses.'

// The value a blank field falls back to, or undefined when nothing is inherited
// for that knob. Shown as its own line under the input rather than a placeholder,
// which reads as "unset" and vanishes as soon as the user types.
export function inheritedValue(
  inherited: Record<string, unknown> | undefined,
  key: string,
): string | undefined {
  const value = inherited?.[key]
  return value === undefined || value === null || value === '' ? undefined : String(value)
}

// Build the InferenceParams override layer from raw form values (only the
// managed keys are read): system_prompt passes through trimmed; numeric knobs
// are coerced, blanks/NaN dropped. Returns undefined when nothing was set.
export function buildParameters(values: Record<string, unknown>): InferenceParams | undefined {
  const params: Record<string, number | string> = {}
  const systemPrompt = values.system_prompt
  if (typeof systemPrompt === 'string' && systemPrompt.trim())
    params.system_prompt = systemPrompt.trim()
  for (const [key] of NUMERIC_PARAMS) {
    const raw = values[key]
    if (raw !== undefined && raw !== null && raw !== '') {
      const n = Number(raw)
      if (!Number.isNaN(n)) params[key] = n
    }
  }
  return Object.keys(params).length ? (params as InferenceParams) : undefined
}

// Field names the AdvancedParams editor manages. A PATCH replaces the whole
// parameters layer, so an edit form preserves any *other* stored keys
// (stop_sequences, provider extras) by filtering on this set.
export const MANAGED_PARAM_NAMES = ['system_prompt', ...NUMERIC_PARAMS.map(([key]) => key)] as const
export const MANAGED_PARAM_KEYS = new Set<string>(MANAGED_PARAM_NAMES)

// Hydrate the string-typed AdvancedParams form fields from a stored parameters
// blob: system prompt passes through, numeric knobs are stringified, other keys
// ignored.
export function paramsToForm(
  params: Record<string, unknown> | undefined | null,
): Record<string, string> {
  const out: Record<string, string> = {}
  if (!params) return out
  if (typeof params.system_prompt === 'string') out.system_prompt = params.system_prompt
  for (const [key] of NUMERIC_PARAMS) {
    const value = params[key]
    if (typeof value === 'number') out[key] = String(value)
  }
  return out
}
