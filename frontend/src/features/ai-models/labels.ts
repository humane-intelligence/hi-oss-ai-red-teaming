// The backend's provider enum, split by whether a URL is normally part of the setup:
// a vendor API resolves its base from the provider (though any row may override it
// with a proxy), the rest are configured around an address.
export const VENDOR_PROVIDERS = ['openai', 'anthropic', 'google', 'cohere', 'aws_bedrock'] as const
export const CUSTOM_PROVIDERS = ['generic', 'huggingface', 'azure'] as const
export const PROVIDERS = [...VENDOR_PROVIDERS, ...CUSTOM_PROVIDERS] as const

// Only `generic` is unusable without one — HF serverless routes through the HF
// router and Azure can take its base from the environment.
export const requiresInferenceEndpoint = (p: (typeof PROVIDERS)[number]) => p === 'generic'

// Which half of the registry a new model belongs to. Chosen once, up front — it
// decides the provider set and whether a URL is part of the form at all.
export type ModelKind = 'provider' | 'custom'
export const isModelKind = (v: string | null): v is ModelKind => v === 'provider' || v === 'custom'
export const providersForKind = (k: ModelKind) =>
  k === 'custom' ? CUSTOM_PROVIDERS : VENDOR_PROVIDERS

// Shared by the chooser dialog and the summary heading the form it leads to, so the
// two can't drift.
export const MODEL_KINDS: Record<ModelKind, { title: string; description: string }> = {
  provider: {
    title: 'Provider API',
    description:
      'One of the vendors the console already knows how to reach. You supply the model id and a key.',
  },
  custom: {
    title: 'Your own endpoint',
    description:
      'You supply the address — a self-hosted model, an OpenAI-compatible service such as OpenRouter, or your Azure or Hugging Face deployment.',
  },
}

export const MODALITIES = ['text', 'image'] as const

// Human-readable labels for the backend's lowercase enum values.
const PROVIDER_LABELS: Record<string, string> = {
  huggingface: 'Hugging Face',
  google: 'Google',
  openai: 'OpenAI',
  anthropic: 'Anthropic',
  azure: 'Azure',
  generic: 'Generic',
  aws_bedrock: 'AWS Bedrock',
  cohere: 'Cohere',
}

const MODALITY_LABELS: Record<string, string> = {
  text: 'Text',
  image: 'Image',
}

export const providerLabel = (p: string) => PROVIDER_LABELS[p] ?? p
export const modalityLabel = (m: string) => MODALITY_LABELS[m] ?? m

// The arrow form is for places with room for one line only (a table cell); where
// there is room, the two directions get their own labelled rows instead.
export const modalitySummary = (input: readonly string[], output: readonly string[]) =>
  `${input.map(modalityLabel).join(', ')} → ${output.map(modalityLabel).join(', ')}`

// For the two places a model is *picked*, where its labels ride along one line of the option: a row
// wearing the full 20 reads as a ~1300-character accessible name. Bounded here rather than in the
// shared picker, which also serves closed lists whose hints are already short. Callers that can
// carry a tooltip pass the full set as well — the visible two are alphabetical, so the one the
// operator cares about is not necessarily among them.
const LABELS_SHOWN = 2
export const modelLabelSummary = (labels: readonly string[]) =>
  labels.length <= LABELS_SHOWN
    ? labels.join(', ')
    : `${labels.slice(0, LABELS_SHOWN).join(', ')} +${labels.length - LABELS_SHOWN}`

const HEALTH_LABELS: Record<string, string> = {
  checking: 'Checking…',
  alive: 'Alive',
  dead: 'Dead',
}

// `null`/absent = never health-checked.
export const healthLabel = (s: string | null | undefined) =>
  s ? (HEALTH_LABELS[s] ?? s) : 'Never checked'

const HEALTH_CLASSES: Record<string, string> = {
  alive: 'text-ok',
  dead: 'text-destructive',
}

export const healthClass = (s: string | null | undefined) =>
  (s && HEALTH_CLASSES[s]) || 'text-muted-foreground'

// Copy for the two things that can be wrong with a declaration, kept here so the badge text and its
// explanation stay one edit apart. On the detail page both render as a `warn` badge plus an
// `InfoHint`: the badge is the marker an operator scans for, the hint is the sentence that makes it
// actionable, and it is a popover rather than a `title` because a touch device produces no hover.
// The list carries the badge alone — a popover per row is the wrong trade, and the row is one click
// from the page that explains it.
export const NO_TEXT_OUTPUT_LABEL = 'no text output'
export const NO_TEXT_OUTPUT_HINT =
  'Conversations need a text reply, so this model cannot be used in one. Widen the output declaration, or pick a different model for the evaluation.'

// "input" earns its six characters on the list, where the cell reads `Text, Image → Text` and a
// bare "unconfirmed" could attach to either side of the arrow.
export const IMAGE_UNCONFIRMED_LABEL = 'image input unconfirmed'
// The hint quotes the provider verbatim, so it needs a name of its own — see `InfoHint`.
export const IMAGE_UNCONFIRMED_HINT_LABEL = 'Why image input is unconfirmed'
// Says what is not known, not what is true: the endpoint may reject the payload shape while the
// model behind it does have vision, and only the operator can tell those apart.
export const imageUnconfirmedHint = (reason: string) =>
  `The last health check sent a test image and the endpoint refused it: "${reason}" — so the image input declared above is unverified, not disproven. Either the endpoint does not accept images at all, or it rejects this request shape.`

// One question, one answer: "can I use this model right now?" — an operator switch
// and a capability that makes dispatch impossible both belong in the same field.
export const modelStatus = (m: { is_disabled: boolean; output_modalities: readonly string[] }) => {
  if (m.is_disabled) return { label: 'disabled', variant: 'err' as const }
  if (!m.output_modalities.includes('text'))
    return { label: NO_TEXT_OUTPUT_LABEL, variant: 'warn' as const }
  return { label: 'enabled', variant: 'ok' as const }
}
