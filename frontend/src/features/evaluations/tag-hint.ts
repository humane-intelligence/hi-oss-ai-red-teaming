export const TAGS_HINT =
  "Context tags describe a conversation to the model, for example language: Polish. “language” is the key and “Polish” is the value. They are added to the model's instructions, so they affect its answers, and they appear in exports so results can be compared. A conversation can have one value per key."

export const LABELS_HINT =
  'Custom tags describe a conversation for organising and reporting, for example language: Polish. “language” is the tag category and “Polish” is the tag. They are never sent to the model, and exports carry them in the “Labels” column. A category can list its tags or take free text, and a conversation has at most one tag per category.'
