import type { AnnotationResponse } from '@/lib/api/types'

export type AnnotationIndex = {
  /** Message id -> the annotations on it, catalog labels first then ad-hoc, each alphabetical. */
  byMessage: Map<string, AnnotationResponse[]>
  /** Annotations whose message is absent from this transcript (superseded, or off-page). */
  unanchored: AnnotationResponse[]
}

/** The label a chip shows. */
export function annotationLabel(annotation: AnnotationResponse): string {
  return annotation.label.name
}

/** The picker's value for an annotation — always its label's id, since every annotation has one. */
export function annotationValue(annotation: AnnotationResponse): string {
  return annotation.label.id
}

/**
 * Map annotations onto transcript rows.
 *
 * Unlike a note, an annotation anchors to exactly one message, so there is no earliest-covered
 * rule — grouping is a plain bucket. An annotation whose message is not in `orderedMessageIds`
 * is kept in `unanchored` rather than dropped: the server keeps listing annotations on
 * superseded messages (the transcript stops rendering those), and a count that silently lost
 * them would understate the labelling.
 */
export function indexAnnotations(
  items: AnnotationResponse[],
  orderedMessageIds: string[],
): AnnotationIndex {
  const visible = new Set(orderedMessageIds)
  const byMessage = new Map<string, AnnotationResponse[]>()
  const unanchored: AnnotationResponse[] = []

  for (const annotation of items) {
    if (!visible.has(annotation.message_id)) {
      unanchored.push(annotation)
      continue
    }
    const bucket = byMessage.get(annotation.message_id)
    if (bucket) bucket.push(annotation)
    else byMessage.set(annotation.message_id, [annotation])
  }

  // Curated labels before an annotator's own, each group alphabetical: the shared vocabulary
  // is the common language, so it reads first, and a stable order stops chips reshuffling as
  // other annotators add theirs.
  for (const bucket of byMessage.values()) {
    bucket.sort((a, b) => {
      if (a.label.is_custom !== b.label.is_custom) return a.label.is_custom ? 1 : -1
      return annotationLabel(a).localeCompare(annotationLabel(b))
    })
  }
  return { byMessage, unanchored }
}
