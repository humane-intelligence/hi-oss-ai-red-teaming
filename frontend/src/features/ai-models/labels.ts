// The backend's provider enum, split by whether a URL is normally part of the setup:
// a vendor API resolves its base from the provider (though any row may override it
// with a proxy), the rest are configured around an address.
export const VENDOR_PROVIDERS = ['openai', 'anthropic', 'google', 'cohere', 'aws_bedrock'] as const
export const CUSTOM_PROVIDERS = ['generic', 'huggingface', 'azure'] as const
export const PROVIDERS = [...VENDOR_PROVIDERS, ...CUSTOM_PROVIDERS] as const

// Only `generic` is unusable without one — HF serverless routes through the HF
// router and Azure can take its base from the environment.
export const requiresInferenceEndpoint = (p: (typeof PROVIDERS)[number]) => p === 'generic'

// Vendors the backend never offers the Internet access switch for, whatever the model id (litellm
// has nothing to translate `web_search_options` into there), so the form can lock it before a
// save instead of letting the 400 explain. OpenAI is per model and Anthropic/Google always have
// one — those stay the backend's call.
const NO_WEB_ACCESS: ReadonlySet<string> = new Set<(typeof PROVIDERS)[number]>([
  'generic',
  'huggingface',
  'azure',
  'cohere',
  'aws_bedrock',
])
export const neverHasWebAccess = (p: string) => NO_WEB_ACCESS.has(p)

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

// What the backend sends is `<routing prefix>/<provider_model_id>`, so an id pasted with the
// prefix from a provider's list reaches the vendor doubled; the save refuses it, and this says so
// before the operator finds out. Examples name a family, not a dated id, so they age slowly —
// except Bedrock, whose ids are dated by construction.
const MODEL_ID_HINTS: Record<string, string> = {
  openai: 'E.g. gpt-4.1 — without the openai/ prefix; the Provider field adds it.',
  anthropic:
    'E.g. claude-sonnet-4-5, with dashes as in Anthropic’s list — without the anthropic/ prefix; the Provider field adds it.',
  google: 'E.g. gemini-2.5-flash — not gemini/… or google/…; the Provider field adds the prefix.',
  cohere: 'E.g. command-r-plus — without the cohere/ prefix; the Provider field adds it.',
  aws_bedrock:
    'The Bedrock model id, e.g. anthropic.claude-3-5-sonnet-20240620-v1:0 — without the bedrock/ prefix.',
  azure: 'Your deployment name — without the azure/ prefix.',
  huggingface:
    'The repo id, e.g. meta-llama/Llama-3.1-8B-Instruct — without the huggingface/ prefix.',
  generic:
    'Whatever the endpoint expects. OpenRouter ids such as anthropic/claude-sonnet-4.5 work here with an OpenRouter endpoint and key.',
}
const OWN_ENDPOINT_HINT = ' With your own endpoint the id goes through as typed.'
export const providerModelIdHint = (p: string, ownEndpoint: boolean) =>
  (MODEL_ID_HINTS[p] ?? 'The id the provider’s API expects.') +
  (ownEndpoint && p !== 'generic' ? OWN_ENDPOINT_HINT : '')

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

// The backend stores a machine reason (`last_health_reason`, ≤64 chars); the words are the console's.
// Only the reasons that persist: a timeout or an unreachable endpoint keeps the check retrying and
// settles as `cold/deadline`. Unknown reasons fall through verbatim, so a new one is never hidden.
const HEALTH_REASON_LABELS: Record<string, string> = {
  'model-not-found':
    'Model not found at the provider — check the model id. If it was copied from a provider list such as OpenRouter, drop the vendor prefix.',
  auth: 'The provider rejected the API key.',
  'bad-request': 'The provider rejected the request — check the model id and parameters.',
  'context-window': 'The probe exceeded the model’s context window.',
  'cold/deadline': 'The endpoint timed out or could not be reached before the wake deadline.',
  error: 'The provider returned an error that could not be classified.',
}
export const healthReasonLabel = (reason: string) => HEALTH_REASON_LABELS[reason] ?? reason

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

// Read beside the switch: a stored `true` the provider no longer honours is the one state the
// gateway refuses every chat in, so it must not read as a plain Yes.
export const webAccessLabel = (m: { web_access: boolean; web_access_supported: boolean }) => {
  if (m.web_access)
    return m.web_access_supported ? 'Yes' : 'Yes — no longer available at this provider'
  return m.web_access_supported ? 'No' : 'Not available at this provider'
}
