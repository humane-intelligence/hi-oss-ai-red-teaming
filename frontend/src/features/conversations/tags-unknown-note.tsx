import { useConversationTagsUnknown } from '@/features/system-preferences/queries'

// Context tags are hidden when the instance switch can't be read, but the backend still follows
// its own setting: without this line, tags saved on the conversation would go out unseen.
export function TagsUnknownNote({ evaluationAllowsTags }: { evaluationAllowsTags: boolean }) {
  const unknown = useConversationTagsUnknown()
  if (!unknown || !evaluationAllowsTags) return null
  return (
    <p role="status" className="text-muted-foreground text-xs">
      Couldn’t check whether context tags are on for this instance, so they are hidden here. Any
      already saved on this conversation may still be sent to the model. Reload the page to retry.
    </p>
  )
}
