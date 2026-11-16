// The contract caps an invite envelope (platform + group) at 100 rows, and `maxItems` isn't
// emitted into `schema.d.ts` — so the number lives here and the dialogs stop the request
// rather than take a 422 that carries no field to attach the message to.
export const MAX_INVITE_ROWS = 100

// The tag-map edges the API enforces (`ConversationTags`). All three are published in the spec
// (`propertyNames.pattern`, `maxProperties`, `additionalProperties.maxLength`) — but
// openapi-typescript emits none of them into `schema.d.ts`, so the dialog carries the numbers to
// keep an invalid edit off the wire. The 10 KiB serialised cap has no JSON Schema form and is
// enforced server-side only, so an oversized set still comes back as a 422.
export const MAX_TAGS = 16
export const MAX_TAG_KEY_LEN = 64
export const MAX_TAG_VALUE_LEN = 512

// The label-category edges the API enforces (`EvaluationLabelCategoryCreate`); like the tag limits,
// openapi-typescript drops `maxLength`/`maxItems`, so the dialog carries the numbers.
export const MAX_LABEL_CATEGORIES = 20
export const MAX_LABEL_VALUES = 50
export const MAX_LABEL_CATEGORY_NAME_LEN = 64
export const MAX_LABEL_VALUE_LEN = 100
// A free-text label value on a conversation group (`GroupLabelsInput`).
export const MAX_GROUP_LABEL_VALUE_LEN = 200
