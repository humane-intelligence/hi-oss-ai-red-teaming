import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiClient } from '@/lib/api/client'
import { unwrap } from '@/lib/api/fetcher'

const completedTasksKey = (conversationId: string) => ['completed-tasks', conversationId] as const

// A red-teamer's checked-off tasks in one conversation, reduced to the set of task ids
// the checklist actually reads. The list is the full (unpaginated) set for the
// conversation, so the checklist derives its state from it. One key + fetch shape for both
// hooks below, which is what makes the group rail read the same cache entries the
// conversation page writes.
function completedTasksQueryOptions(
  conversationId: string,
  opts?: { suppressErrorToast?: boolean },
) {
  return {
    queryKey: completedTasksKey(conversationId),
    enabled: conversationId !== '',
    // The group view reads one of these per member, so a single outage would toast up to
    // `max_conversation_group_size` times; the rail reports that failure itself. The
    // conversation page has one read and no such surface, so it keeps the global toast.
    meta: opts?.suppressErrorToast ? { suppressErrorToast: true } : undefined,
    queryFn: async () => {
      const completions = unwrap(
        await apiClient.GET('/api/v1/conversations/{conversation_id}/completed-tasks', {
          params: { path: { conversation_id: conversationId } },
        }),
      )
      return completions.map((c) => c.task_id)
    },
  }
}

export function useCompletedTasks(conversationId: string) {
  return useQuery(completedTasksQueryOptions(conversationId))
}

// Every member conversation's completed tasks, for the group view's per-conversation
// rows. One read per member instead of the group roll-up endpoint, so the rail can derive
// K/N from the same sets that drive the checkboxes.
//
// Why that trade is sound: the fan-out is bounded by `max_conversation_group_size` (4) by
// config, not by hope, and these reads are a sibling of the group response rather than a
// step deeper — the rail's rows already wait on the task list, one level further out. So
// nothing renders later than it did when this was one roll-up call.
export function useMembersCompletedTasks(conversationIds: string[]) {
  const queries = useQueries({
    queries: conversationIds.map((id) =>
      completedTasksQueryOptions(id, { suppressErrorToast: true }),
    ),
  })
  return {
    // Aligned with `conversationIds` by index. A member that hasn't resolved reads as
    // empty and is indistinguishable from one with nothing completed, so `state` — not
    // this map — decides whether a count may be shown.
    completedTaskIdsByConversation: conversationIds.map(
      (_id, i) => new Set(queries[i]?.data ?? []),
    ),
    // `error` outranks `loading`: one failed member means the roll-up can't be completed
    // at all, even while its siblings are still in flight.
    state: queries.some((q) => q.isError)
      ? ('error' as const)
      : queries.some((q) => q.isPending)
        ? ('loading' as const)
        : ('ready' as const),
  }
}

// Toggle one task's completion in a conversation. Optimistically patches the
// conversation's completed-task-id list (add on check, drop on uncheck) and rolls back on
// error; on settle it refetches that conversation's set, which is what the rail derives
// K/N from.
// `conversationId` travels in the variables, not the hook argument: the group page owns
// this mutation for every member conversation at once, so the rail can stay presentational
// behind an `onToggle` prop.
export function useToggleTaskCompletion() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({
      conversationId,
      taskId,
      next,
    }: {
      conversationId: string
      taskId: string
      next: boolean
    }) => {
      const opts = { params: { path: { conversation_id: conversationId, task_id: taskId } } }
      return next
        ? unwrap(
            await apiClient.PUT(
              '/api/v1/conversations/{conversation_id}/completed-tasks/{task_id}',
              opts,
            ),
          )
        : unwrap(
            await apiClient.DELETE(
              '/api/v1/conversations/{conversation_id}/completed-tasks/{task_id}',
              opts,
            ),
          )
    },
    onMutate: async ({ conversationId, taskId, next }) => {
      const key = completedTasksKey(conversationId)
      await qc.cancelQueries({ queryKey: key })
      // Functional, per-task patch (add on check, drop on uncheck) so concurrent
      // toggles of *different* tasks don't clobber one another — there's no shared
      // snapshot to race, which is what lets the UI disable only the in-flight row.
      // Keyed per conversation, so concurrent toggles across the group's members are
      // independent for the same reason.
      qc.setQueryData<string[]>(key, (cur) => {
        const without = (cur ?? []).filter((id) => id !== taskId)
        return next ? [...without, taskId] : without
      })
    },
    onError: (_e, { conversationId, taskId, next }) => {
      // Invert only this task's optimistic change; other in-flight toggles stay put.
      // onSettled's refetch reconciles the full set (incl. a cold-cache first load).
      qc.setQueryData<string[]>(completedTasksKey(conversationId), (cur) => {
        const without = (cur ?? []).filter((id) => id !== taskId)
        return next ? without : [...without, taskId]
      })
    },
    onSettled: (_d, _e, { conversationId }) => {
      qc.invalidateQueries({ queryKey: completedTasksKey(conversationId) })
    },
  })
}
